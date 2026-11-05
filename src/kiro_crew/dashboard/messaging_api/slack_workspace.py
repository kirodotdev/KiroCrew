"""The Slack workspace routes: pins, reactions and recent messages on a tracked
channel, a user's profile, and the app manifest.

``POST /api/slack/pins``, ``POST /api/slack/reactions``, ``POST /api/slack-history``,
``POST /api/slack-profile`` and ``GET /api/slack/manifest``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.messaging import (
        _EMOJI_NAME_RE,
        CHANNEL_ID_RE,
        CHANNEL_MAX_LEN,
        DashboardState,
        _is_slack_ts,
        _sel,
        body_not_object_error,
        logger,
    )


async def api_slack_pins(request: web.Request) -> web.Response:
    """POST /api/slack/pins — pin/unpin/list pins on a tracked channel.

    Server-side proxy so callers never need the Slack bot token. The gateway
    holds the token in ``state.slack_client``; this route enforces the same
    tracked-channel allowlist and SEL audit logging as the other Slack routes.

    Body: {"channel": "C...", "action": "add"|"remove"|"list", "ts": "..."}
    (``ts`` required for add/remove, ignored for list).
    """
    # circular import: slack.handler imports from dashboard.* at module load
    from kiro_crew.slack.handler import is_tracked_channel  # noqa: F811

    state: DashboardState = request.app["state"]
    slack = state.slack_client
    if not slack:
        return web.json_response({"ok": True, "skipped": "no_slack"})
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    not_object = body_not_object_error(body)
    if not_object is not None:
        return not_object

    action = body.get("action", "")
    if action not in ("add", "remove", "list"):
        return web.json_response({"error": "action must be 'add', 'remove', or 'list'"}, status=400)
    channel = body.get("channel", "")
    if not isinstance(channel, str):
        return web.json_response({"error": "invalid channel ID format"}, status=400)
    channel = channel.strip()
    if not channel or len(channel) > CHANNEL_MAX_LEN or not CHANNEL_ID_RE.match(channel):
        return web.json_response({"error": "invalid channel ID format"}, status=400)

    ts = body.get("ts", "")
    if action in ("add", "remove"):
        if not _is_slack_ts(ts):
            return web.json_response(
                {"error": "ts must be a Slack timestamp string like '1712793600.123456'"},
                status=400,
            )

    if not is_tracked_channel(channel):
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="slack_pins",
            tool_kind="slack",
            outcome="denied",
            downstream_service="slack",
            resources=f"channel={channel} action={action}",
        )
        return web.json_response(
            {"error": f"channel {channel} not in tracked channels"}, status=403
        )

    try:
        result: dict[str, Any] = {"ok": True}
        if action == "add":
            await slack.add_pin(channel, ts)
        elif action == "remove":
            await slack.remove_pin(channel, ts)
        else:
            # Pinned messages may contain content originally posted by
            # LLM-controlled agents; redact each text field before returning
            # it to the caller (same output contract as send_message).
            pins = await slack.list_pins(channel)
            for pin in pins:
                from kiro_crew.security import redact

                safe_text = redact(pin.get("text", ""))
                pin["text"] = safe_text
            result["pins"] = pins
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="slack_pins",
            tool_kind="slack",
            outcome="completed",
            downstream_service="slack",
            resources=f"channel={channel} action={action}",
        )
        return web.json_response(result)
    except Exception as e:
        from kiro_crew.security import redact

        safe_error = redact(str(e))
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="slack_pins",
            tool_kind="slack",
            outcome="error",
            downstream_service="slack",
            resources=f"channel={channel} action={action}",
            error=safe_error,
        )
        return web.json_response({"error": safe_error}, status=502)


async def api_slack_reactions(request: web.Request) -> web.Response:
    """POST /api/slack/reactions — add/remove an emoji reaction on a tracked channel.

    Server-side proxy so callers never need the Slack bot token. Mirrors the
    pins route: tracked-channel allowlist + SEL audit + server-held token.

    Body: {"channel": "C...", "ts": "...", "emoji": "white_check_mark",
           "action": "add"|"remove"}
    """
    # circular import: slack.handler imports from dashboard.* at module load
    from kiro_crew.slack.handler import is_tracked_channel  # noqa: F811

    state: DashboardState = request.app["state"]
    slack = state.slack_client
    if not slack:
        return web.json_response({"ok": True, "skipped": "no_slack"})
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    not_object = body_not_object_error(body)
    if not_object is not None:
        return not_object

    action = body.get("action", "")
    if action not in ("add", "remove"):
        return web.json_response({"error": "action must be 'add' or 'remove'"}, status=400)
    channel = body.get("channel", "")
    if not isinstance(channel, str):
        return web.json_response({"error": "invalid channel ID format"}, status=400)
    channel = channel.strip()
    if not channel or len(channel) > CHANNEL_MAX_LEN or not CHANNEL_ID_RE.match(channel):
        return web.json_response({"error": "invalid channel ID format"}, status=400)
    ts = body.get("ts", "")
    if not _is_slack_ts(ts):
        return web.json_response(
            {"error": "ts must be a Slack timestamp string like '1712793600.123456'"},
            status=400,
        )
    emoji = body.get("emoji", "")
    if not isinstance(emoji, str):
        return web.json_response({"error": "invalid emoji name"}, status=400)
    emoji = emoji.strip()
    if not emoji or not _EMOJI_NAME_RE.match(emoji):
        return web.json_response({"error": "invalid emoji name"}, status=400)

    if not is_tracked_channel(channel):
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="slack_reactions",
            tool_kind="slack",
            outcome="denied",
            downstream_service="slack",
            resources=f"channel={channel} action={action}",
        )
        return web.json_response(
            {"error": f"channel {channel} not in tracked channels"}, status=403
        )

    try:
        if action == "add":
            await slack.add_reaction(channel, ts, emoji, raise_on_error=True)
        else:
            await slack.remove_reaction(channel, ts, emoji, raise_on_error=True)
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="slack_reactions",
            tool_kind="slack",
            outcome="completed",
            downstream_service="slack",
            resources=f"channel={channel} action={action} emoji={emoji}",
        )
        return web.json_response({"ok": True})
    except Exception as e:
        from kiro_crew.security import redact

        safe_error = redact(str(e))
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="slack_reactions",
            tool_kind="slack",
            outcome="error",
            downstream_service="slack",
            resources=f"channel={channel} action={action} emoji={emoji}",
            error=safe_error,
        )
        return web.json_response({"error": safe_error}, status=502)


def _missing_scope_message(needed: str) -> str:
    """Build an actionable missing_scope message, naming the scope(s) when known."""
    # Slack's ``needed`` field may name several comma-separated scopes.
    scopes = [s.strip() for s in needed.split(",") if s.strip()] if needed else []
    if scopes:
        joined = ", ".join(scopes)
        noun = "OAuth scope" if len(scopes) == 1 else "OAuth scopes"
        scope_clause = f"the {joined} {noun}"
        add_clause = f"add {joined} to"
    else:
        scope_clause = "an OAuth scope"
        add_clause = "add the required scope to"
    return (
        f"This Slack action requires {scope_clause}, which is not granted to this app. "
        "Reinstall the app after granting the required permissions in the Slack Dashboard. "
        f"Alternatively, {add_clause} the app manifest and recreate the app by following "
        "the steps in docs/guides/slack-setup.md."
    )


async def api_slack_profile(request: web.Request) -> web.Response:
    """POST /api/slack-profile — read a Slack user's profile."""
    from kiro_crew.security import redact
    from kiro_crew.validation import USER_ID_RE  # noqa: F811

    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    not_object = body_not_object_error(body)
    if not_object is not None:
        return not_object

    raw_user = body.get("user", "")
    if not isinstance(raw_user, str):
        return web.json_response({"error": "user must be a string"}, status=400)
    user_id = raw_user.strip()
    if not user_id:
        return web.json_response({"error": "user required"}, status=400)
    # Validate format first, then redact (#2)
    if not USER_ID_RE.match(user_id):
        return web.json_response({"error": "invalid user ID format"}, status=400)
    user_id = redact(user_id)

    # Authorization first (deny-by-default) — reject before any side effects
    from kiro_crew.slack.handler import is_allowed_user  # noqa: F811

    if not is_allowed_user(user_id):
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="read_slack_profile",
            outcome="denied",
            downstream_service="slack",
            resources=f"user={user_id}",
        )
        return web.json_response({"error": "user not in allowlist"}, status=403)

    if not state.slack_client:
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="read_slack_profile",
            outcome="error",
            downstream_service="slack",
            resources=f"user={user_id} reason=slack_not_connected",
        )
        return web.json_response({"error": "Slack not connected"}, status=503)

    # Rate limiting: max 5 profile lookups per minute (#5)
    # Only counts authorized requests — unauthorized 403s don't consume slots
    now = time.monotonic()
    history: list[float] = getattr(state, "_profile_lookup_times", [])
    history = [t for t in history if now - t < 60]
    if len(history) >= 5:
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="read_slack_profile",
            outcome="denied",
            downstream_service="slack",
            resources=f"user={user_id} reason=rate_limit",
        )
        return web.json_response(
            {"error": "rate limit exceeded — max 5 profile lookups per minute"}, status=429
        )
    history.append(now)
    state._profile_lookup_times = history  # type: ignore[attr-defined]

    try:
        profile = await state.slack_client.get_user_profile(user_id)
    except Exception as exc:
        from slack_sdk.errors import SlackApiError  # noqa: F811

        if isinstance(exc, SlackApiError):
            response = exc.response  # type: ignore[attr-defined]
            slack_error = str(response.get("error", "") or "") if response else ""
            if slack_error == "missing_scope":
                needed = str(response.get("needed", "") or "") if response else ""
                logger.warning(
                    "slack-profile: missing_scope (needed=%s) for %s", needed or "?", user_id
                )
                _sel().log_tool_invocation(
                    session_key="dashboard",
                    tool_name="read_slack_profile",
                    outcome="error",
                    downstream_service="slack",
                    resources=f"user={user_id} reason=missing_scope needed={needed}",
                )
                needed = redact(needed)
                return web.json_response({"error": _missing_scope_message(needed)}, status=403)
        logger.exception("slack-profile: failed for %s", user_id)
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="read_slack_profile",
            outcome="error",
            downstream_service="slack",
            resources=f"user={user_id}",
        )
        return web.json_response({"error": "Slack API error"}, status=502)

    # Redact free-form profile fields that could contain prompt-injection
    for key in list(profile):
        val = profile[key]
        if isinstance(val, str) and key not in ("id",):
            val = redact(val)
            profile[key] = val

    _sel().log_tool_invocation(
        session_key="dashboard",
        tool_name="read_slack_profile",
        outcome="completed",
        downstream_service="slack",
        resources=f"user={user_id}",
    )
    return web.json_response({"profile": profile})


async def api_slack_history(request: web.Request) -> web.Response:
    """POST /api/slack-history — recent messages in a tracked channel or one thread.

    MCP-only (the ``read_slack_history`` tool). Same tracked-channel allowlist as
    the pins route, plus the inbound gate's channels policy and its workspace
    origin check. A message's text is returned only when that gate admits its
    sender (the owner); anyone else's comes back with no text, so this read lets no
    sender reach an agent that could not already message it.

    Body: {"channel": "C...", "thread_ts": "...", "oldest": "...", "limit": 1-100}
    (all but ``channel`` optional; ``limit`` defaults to 20).
    """
    from kiro_crew.messaging.identity import channel_inbound_permitted
    from kiro_crew.platform import current_context
    from kiro_crew.security import redact
    from kiro_crew.slack.channel_policy import channel_refusal
    from kiro_crew.slack.handler import is_allowed_user, is_tracked_channel  # noqa: F811
    from kiro_crew.slack.team_ids import slack_team_id_for

    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    not_object = body_not_object_error(body)
    if not_object is not None:
        return not_object

    channel = body.get("channel", "")
    if (
        not isinstance(channel, str)
        or len(channel) > CHANNEL_MAX_LEN
        or not CHANNEL_ID_RE.match(channel)
    ):
        return web.json_response(
            {"error": "invalid channel ID format", "code": "invalid_channel"}, status=400
        )
    thread_ts = body.get("thread_ts") or None
    oldest = body.get("oldest") or None
    if not all(_is_slack_ts(ts) for ts in (thread_ts, oldest) if ts is not None):
        return web.json_response(
            {"error": "thread_ts and oldest must be Slack timestamps", "code": "invalid_ts"},
            status=400,
        )
    limit = body.get("limit", 20)
    if type(limit) is not int or not 1 <= limit <= 100:
        return web.json_response(
            {"error": "limit must be an integer 1-100", "code": "invalid_limit"}, status=400
        )

    resources = f"channel={channel} thread_ts={thread_ts or ''}"
    if not is_tracked_channel(channel):
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="read_slack_history",
            outcome="denied",
            downstream_service="slack",
            resources=resources,
        )
        return web.json_response(
            {"error": f"channel {channel} not in tracked channels", "code": "channel_not_tracked"},
            status=403,
        )
    # The inbound gate's channels policy too: a channel it refuses is read by no one.
    refused = channel_refusal(channel, operation="slack.history", caller="read_slack_history")
    if refused or not await channel_inbound_permitted("slack"):
        return web.json_response(
            {"error": refused or "channels policy denies slack", "code": "channel_refused"},
            status=403,
        )
    if not state.slack_client:
        return web.json_response(
            {"error": "Slack not connected", "code": "slack_not_connected"}, status=503
        )
    # And the inbound origin gate on the channel's own workspace, so a workspace
    # dropped from slack.allowed_enterprise_ids is read by no one either. retry:
    # one failed lookup must not deny every later read until a restart.
    await state.slack_client.ensure_channel_team(channel, retry=True)
    if not current_context().slack_gate.check_message_origin(slack_team_id_for(channel)):
        return web.json_response(
            {"error": "the channel's workspace is not allowed", "code": "workspace_not_allowed"},
            status=403,
        )

    try:
        raw = await state.slack_client.fetch_history(
            channel, limit, thread_ts=thread_ts, oldest=oldest
        )
    except Exception as e:
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="read_slack_history",
            outcome="error",
            downstream_service="slack",
            resources=resources,
        )
        # Never an empty error (a timeout's str() is ""): the tool would read it as "no messages".
        error = redact(str(e)) or type(e).__name__
        return web.json_response({"error": error, "code": "history_read_failed"}, status=502)

    messages = []
    for m in raw:
        user = str(m.get("user") or "")
        admitted = is_allowed_user(user)
        text = redact(str(m.get("text") or "")) if admitted else ""
        messages.append({"user": user, "ts": m.get("ts", ""), "admitted": admitted, "text": text})
    _sel().log_tool_invocation(
        session_key="dashboard",
        tool_name="read_slack_history",
        outcome="completed",
        downstream_service="slack",
        resources=f"{resources} count={len(messages)}",
    )
    return web.json_response({"messages": messages})


async def api_slack_manifest(request: web.Request) -> web.Response:
    """GET /api/slack/manifest — rendered Slack app manifest + create URL.

    Mirrors ``kirocrew manifest --url`` so the settings UI can offer one-click
    Slack app creation without the CLI: the bundled template gets the user's
    alias substituted, and the comment-stripped YAML is URL-encoded into
    Slack's new-app deep link. ``command`` is the slash command that manifest
    registers. The gateway answers only ``/{slack.command}`` and this endpoint
    writes no config, so a caller that passes ``?alias=`` gets the value its
    ``kirocrew config set slack.command`` step needs. With no alias, the
    endpoint renders for this install: the alias the saved ``slack.command`` was
    generated from, and that command, so a manifest copied to update an existing
    app keeps its slash command. Serves only the public template — no secrets.
    """
    from kiro_crew import slack_manifest
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.slack.features import manifest_features

    try:
        cfg = KiroCrewConfig.load()
    except Exception:
        cfg = None
    explicit = request.query.get("alias", "").strip()
    # With no ?alias, render for THIS install: the alias its saved slash command
    # was generated from, and that command itself, so the manifest an existing
    # app is updated from keeps the name and the /command the gateway answers.
    # The default alias stays non-identifying: $USER is a host account name and
    # is never volunteered to an authenticated client.
    saved_command = str(getattr(getattr(cfg, "slack", None), "command", "") or "")
    alias = explicit or slack_manifest.alias_for_command(saved_command)
    if not slack_manifest.valid_alias(alias):
        return web.json_response({"error": "invalid alias"}, status=400)
    command = None if explicit else saved_command or None
    # The SAME switches `kirocrew manifest` renders for: the manifest a user
    # copies here must ask Slack for what the gateway will use.
    try:
        features = manifest_features(cfg) if cfg is not None else frozenset()
    except Exception:
        features = frozenset()
    try:
        rendered = slack_manifest.render(alias, features=features, command=command)
        create_url = slack_manifest.deep_link(alias, features)
    except FileNotFoundError:
        return web.json_response({"error": "manifest template missing"}, status=500)
    return web.json_response(
        {
            "alias": alias,
            "manifest": rendered,
            "create_url": create_url,
            "command": command or slack_manifest.slash_command(alias),
            "features": [f for f in slack_manifest.FEATURES if f in features],
        }
    )
