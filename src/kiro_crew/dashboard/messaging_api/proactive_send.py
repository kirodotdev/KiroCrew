"""Proactive sends: what ``POST /api/send-message`` reads, its fallback legs, its
audit row and its answer, and the in-place edit and delete routes.

The route itself stays in the facade: it redacts and authorizes the Slack target
and injects into an origin session between the reading and the delivery here.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.messaging import (
        _CHANNEL_TYPE_RE,
        _MAX_BLOCKS,
        _MAX_WALK_DEPTH,
        _RESERVED_SESSION_TARGETS,
        _SEND_MESSAGE_CHANNEL_TYPES,
        _SLACK_ONLY_BODY_FIELDS,
        _SLACK_SECTION_TEXT_MAX,
        BLOCKS_REMOTE_MEDIA_ERROR,
        CHANNEL_ID_RE,
        CHANNEL_MAX_LEN,
        LINK_WINDOW_SECS,
        OPTIONS_FALLBACK_TEXT,
        SESSION_LINK_ACTION,
        SLACK_NAMESPACE,
        DashboardState,
        KiroCrewConfig,
        PostedOptions,
        _blocks_request_remote_media,
        _channel_delivery_key,
        _deliver_channel_dm,
        _deliver_to_channel,
        _is_slack_ts,
        _sel,
        _send_to_channel_target,
        build_options_blocks,
        dashboard_slot_key,
        format_overflow,
        generate_token,
        logger,
        mint_options_token,
        redact_credentials,
        redact_exfiltration_urls,
        redact_for_display,
        remember_slack_options,
        slack_options_owner_key,
    )


def _redact_all(value: str) -> str:
    """Both outbound redactors as one callable, in the canonical order.

    ``redact_for_display`` re-runs its redactor over each normalised form, so it
    needs the pair behind a single call rather than two sequential passes.
    """
    value, _ = redact_exfiltration_urls(value)
    value, _ = redact_credentials(value)
    return value


def _sanitize_blocks(
    blocks: list[dict],
    *redactors: Any,
    display_form: bool = False,
) -> list[dict]:
    """Walk Block Kit blocks and sanitize all strings (both keys and values).

    Block Kit structural keys (type, text, mrkdwn, etc.) pass through
    sanitizers unchanged since they don't match hostile patterns.

    With ``display_form=True`` each string is scanned through
    :func:`redact_for_display` (composing the redactors via ``_redact_all``)
    rather than by the literal redactors alone. That is the SAME floor the text
    path uses, and it is what catches a credential split across Block Kit markup
    — ``AKIA`` in one run and the rest bolded in the next — which the literal
    scan sees only as fragments. Block text a caller controls is LLM-authored,
    so it is exactly where such a split arrives.
    """
    from copy import deepcopy  # noqa: F811

    def _redact_str(s: str) -> str:
        if display_form:
            s, _ = redact_for_display(s, _redact_all)
            return s
        for fn in redactors:
            s, _ = fn(s)
        return s

    def _walk(obj: Any, depth: int = 0) -> Any:
        if depth > _MAX_WALK_DEPTH:
            if isinstance(obj, str):
                return _redact_str(obj)
            if isinstance(obj, (dict, list)):
                return {} if isinstance(obj, dict) else []
            return obj  # scalars (int, bool, None) are safe
        if isinstance(obj, str):
            return _redact_str(obj)
        if isinstance(obj, dict):
            return {_redact_str(k): _walk(v, depth + 1) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_walk(item, depth + 1) for item in obj]
        return obj

    return _walk(deepcopy(blocks[:_MAX_BLOCKS]))


def _resolve_session_target(
    state: DashboardState, target: str, caller_session: str
) -> tuple[str, str] | tuple[None, None]:
    """Resolve a session target to a dashboard slot key and job name.

    ``target="origin"`` looks up the cron job that owns *caller_session*
    and returns ``(session_key, job_name)``.
    Returns ``(None, None)`` if the origin session can't be resolved
    (non-"origin" target, non-cron caller, unknown job, or cron with no
    originating session_key — e.g. one created from the dashboard UI).

    Note: ``target="slack"`` is NOT handled here — it is intercepted in
    ``api_send_message`` and converted to an explicit fall-through to the
    Slack DM path, so it never reaches this resolver.
    """
    if target != "origin":
        return None, None  # only "origin" is allowed — reject arbitrary slot keys
    # caller_session is "cron:{job_id}" or "cron:{job_id}:{run_id}" (stateless)
    if not caller_session.startswith("cron:"):
        return None, None
    cron_id = caller_session.removeprefix("cron:").split(":")[0]
    jobs = state.crons.list_jobs(include_disabled=True)
    job = next((j for j in jobs if j.id == cron_id), None)
    if not job or not job.session_key:
        return None, None
    # session_key is e.g. "dashboard:chat-3-1712793600" but slot names
    # don't have the "dashboard:" prefix
    slot_key = job.session_key.removeprefix("dashboard:")
    return slot_key, job.name


def _session_link_blocks(url: str) -> list[dict[str, Any]]:
    """One Block Kit ``actions`` block with an "Open session" link button.

    A URL button opens *url* in the user's browser directly, so it needs no
    interaction handler beyond the no-op ack (see ``SESSION_LINK_ACTION``).

    ``api_send_message`` attaches this block to the SAME Slack message the caller
    is already sending whenever that message carries Block Kit blocks (caller
    blocks, or a plain-text send upgraded to a text section), so an opted-in send
    is ONE message and ONE notification. It falls back to posting these blocks as
    a trailing follow-up message only when the primary message cannot carry them
    (a caller using ``options``) or when the merged post was rejected -- so a link
    Slack rejects never fails the message the caller actually asked to send.

    The trailing ``context`` line states the sign-in window up front: the button
    outlives its embedded credential (``LINK_WINDOW_SECS``), and status messages
    are often read late, so without the hint a late tap lands on the sign-in
    wall as a surprise. The wall itself offers recovery (send a sign-in link
    from a signed-in device, or ``kirocrew token``), but expectation-setting at
    the message is what keeps the late tap from reading as a dead end.
    """
    minutes = max(1, LINK_WINDOW_SECS // 60)
    return [
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Open session"},
                    "url": url,
                    "action_id": SESSION_LINK_ACTION,
                }
            ],
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"Signs you in if opened within ~{minutes} min of this "
                        "message; after that you may be asked to sign in."
                    ),
                }
            ],
        },
    ]


async def _resolve_session_link_url(
    state: DashboardState, caller_session: str, declared_session: str
) -> str:
    """The presigned deep link to the CALLER's own dashboard session, or ``""``.

    The session is resolved SERVER-SIDE, never from a body field: a cron caller
    links to the origin session that spawned it (``_channel_delivery_key`` reads
    the job's stored ``session_key``), and every other caller is identified by
    the ``X-Session-Key`` header, which ``token_auth`` kernel-attests against the
    AF_UNIX peer. So the link can only ever point at the session that actually
    sent the message -- a wrong-session link is structurally impossible.

    ``dashboard_slot_key`` maps that session key to the tab that displays it and
    answers ``""`` when no tab does; the button is omitted rather than pointing
    ``?sid=`` at a key the SPA cannot resolve.

    The origin follows ``slack.allowlist.send_dashboard_link``'s convention (the
    shared ``dashboard_link_origin`` helper): the live tunnel URL when
    ``slack.use_tunnel_url`` is set and one is connected, otherwise the configured
    dashboard origin. A presigned ``token_auth`` click token is appended so the
    link authenticates off-host; the button is delivered to the owner DM only (see
    ``api_send_message``), so the token never reaches a shared channel.

    Returns ``""`` -- and the caller omits the button -- for a headless caller (no
    resolvable session / no open tab) or when no usable origin exists (no tunnel
    and no dashboard origin). Never raises: a config-read failure degrades to no
    button, because the message must still go.
    """
    session_key = _channel_delivery_key(state, caller_session, declared_session)
    # The dashboard TAB that displays this conversation, or "" when none does.
    # ``dashboard_slot_key`` is the real mapping: a bare ``removeprefix(
    # "dashboard:")`` leaves a channel-born key unchanged -- an inbound Slack DM
    # runs under ``slack:<ts>`` while its tab is ``slack_<ts>``, and a cron whose
    # origin is a channel session carries that channel key verbatim -- so the
    # SPA's ``?sid=`` would never match and the button would open a missing
    # session. It also answers "" when the conversation has no open tab, and then
    # we OMIT the button rather than deep-link to a tab that does not exist.
    slot_key = dashboard_slot_key(session_key)
    if not slot_key:
        return ""
    # A dashboard-prefixed key resolves to a slot key UNCONDITIONALLY:
    # ``has_dashboard_surface`` short-circuits True for any ``dashboard:`` key
    # before consulting the surface registry, so a dashboard-born session whose
    # tab has since been closed still yields a slot key here. Minting a link for
    # it hands the owner a button that opens "Session not found." Confirm a LIVE
    # slot still exists (``get_slot`` returns None for a closed/absent tab) and
    # omit the button otherwise -- the message still goes, just without a link
    # that leads nowhere.
    if state.get_slot(slot_key) is None:
        return ""
    # Lazy imports: keep the tunnel and backfill modules off this handler
    # module's import path (it loads at gateway boot) and matches the file's
    # other deferred imports. The config read is paid only on this opt-in path.
    from kiro_crew.dashboard.chat_backfill import session_deep_link
    from kiro_crew.dashboard.urls import tunnel_origin_if_opted_in

    try:
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
    except Exception:
        logger.debug("send_message: session-link config load failed", exc_info=True)
        return ""
    # The tunnel-vs-dashboard decision lives in one shared helper (the same one
    # ``chat_mirror`` and ``chat_slack`` use), not re-spelled here.
    tunnel_url = tunnel_origin_if_opted_in(cfg.slack.use_tunnel_url)
    # Presigned link, the same door as ``slack.allowlist.send_dashboard_link``: a
    # raw ``/chat?sid=`` link is refused by ``token_auth`` (a valid token is
    # required on every request), so off-host -- the tunnel arm's whole reason to
    # exist -- it lands on the sign-in wall instead of the session. Mint the owner
    # a click token so the link authenticates. ``api_send_message`` posts this
    # button to the OWNER DM only, so the token never rides a shared channel --
    # the same DM-only rule ``send_dashboard_link`` keeps. No owner id means no DM
    # to post to, so no token is minted (the button is not posted either).
    token = generate_token(state.owner_id) if state.owner_id else ""
    return session_deep_link(cfg.dashboard.url, slot_key, tunnel_url=tunnel_url, token=token)


async def api_delete_message(request: web.Request) -> web.Response:
    """POST /api/delete-message — delete a bot-authored Slack message."""
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    channel = body.get("channel", "").strip()
    ts = body.get("ts", "").strip()
    if not channel or not ts:
        return web.json_response({"error": "channel and ts required"}, status=400)
    slack = state.slack_client
    if not slack:
        return web.json_response({"error": "Slack not connected"}, status=503)
    try:
        await slack.delete_message(channel, ts)
    except Exception as e:
        safe_error = str(e).split("\n")[0][:200]
        safe_error, _ = redact_credentials(safe_error)
        safe_error, _ = redact_exfiltration_urls(safe_error)
        return web.json_response({"error": f"Delete failed: {safe_error}"}, status=502)
    return web.json_response({"ok": True})


async def api_update_message(request: web.Request) -> web.Response:
    """POST /api/update-message — edit a bot-authored Slack message in place.

    The egress twin of ``api_delete_message``: same "the bot's own message"
    addressing, but it PUBLISHES replacement content, so the outbound floor is
    ``api_send_message``'s — ``redact_for_display`` over the text and
    ``_sanitize_blocks`` over the blocks, before either reaches Slack.

    Authorization is per target kind. A ROOM must be in the tracked-channel
    allowlist, which is the operator's revocation lever rather than only a
    first-contact check — without it a message the bot authored while the channel
    was tracked would stay a writable slot in it after the operator revoked egress.
    A DM must be the CURRENT owner's, resolved through the same
    ``open_dm(owner_id)`` the send path uses, and fails closed when no owner is
    configured or the lookup raises. Admitting every ``D`` channel on its prefix
    would leave a FORMER owner's DM permanently writable, since ``owner_id``
    changes and the DM channel id does not.

    That is one notch stricter than the ``file_send`` Slack leg
    (``dashboard/upload_destination.py::resolve_slack``), which still passes DMs on
    the prefix. Deliberate: this endpoint publishes replacement content into a
    message that is already there, so a wrong audience is not merely a new message
    they can ignore.
    """
    # circular import: slack.handler imports from dashboard.* at module load
    from kiro_crew.slack.handler import is_tracked_channel  # noqa: F811

    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    channel = body.get("channel", "")
    if not isinstance(channel, str):
        return web.json_response(
            {"error": "invalid channel ID format", "code": "invalid_channel"}, status=400
        )
    channel = channel.strip()
    if not channel or len(channel) > CHANNEL_MAX_LEN or not CHANNEL_ID_RE.match(channel):
        return web.json_response(
            {"error": "invalid channel ID format", "code": "invalid_channel"}, status=400
        )
    ts = body.get("ts", "")
    if not _is_slack_ts(ts):
        return web.json_response(
            {
                "error": "ts must be a Slack timestamp string like '1712793600.123456'",
                "code": "invalid_ts",
            },
            status=400,
        )
    text = body.get("text", "")
    if not isinstance(text, str):
        return web.json_response(
            {"error": "text must be a string", "code": "invalid_text"}, status=400
        )
    blocks = body.get("blocks")
    if blocks is not None and not isinstance(blocks, list):
        return web.json_response(
            {"error": "blocks must be a list", "code": "invalid_blocks"}, status=400
        )
    # The edit path publishes replacement content, so it carries the SAME
    # server-fetched-media boundary as api_send_message: without this, an
    # agent could send clean blocks and then EDIT remote media into the
    # message — Slack fetches Block Kit media regardless of unfurl flags.
    if isinstance(blocks, list) and _blocks_request_remote_media(blocks):
        return web.json_response(
            {
                "error": (
                    "agent-supplied Block Kit cannot contain image/video blocks "
                    "or image_url/thumbnail_url/video_url fields because Slack "
                    "fetches that media without a recipient click"
                ),
                "code": BLOCKS_REMOTE_MEDIA_ERROR,
            },
            status=400,
        )
    if not text and not blocks:
        return web.json_response(
            {"error": "text or blocks required", "code": "content_required"}, status=400
        )
    # A DM is authorized by IDENTITY, a room by the tracked-channel allowlist --
    # and a DM must be the CURRENT owner's, not merely D-prefixed. Passing every
    # `D...` on the prefix alone would leave any DM this bot ever posted in a
    # writable slot forever, including a FORMER owner's: `owner_id` changes, the
    # old DM channel id does not, and an edit publishes new agent-authored text
    # into it. So the prefix is a routing fact, never an authorization one.
    #
    # For rooms, tracking is the operator's revocation lever (see
    # api_send_message's 403: "Add it to config.json ... restart the gateway"), not
    # just a first-contact check -- a message this bot authored while the channel
    # was tracked must not remain writable after the operator revoked egress.
    #
    # Both refused before any content processing, matching api_send_message's
    # "Authorization gates (before any side effects)" ordering.
    if channel.startswith("D"):
        # Resolved through the same `open_dm(state.owner_id)` the send path uses
        # (see the send_message Slack leg), so the two agree on who the owner is.
        # Fail CLOSED when there is no owner configured or the lookup raises: an
        # unresolvable owner means we cannot prove this DM is theirs.
        owner_dm = ""
        if state.owner_id and state.slack_client:
            try:
                owner_dm = await state.slack_client.open_dm(state.owner_id)
            except Exception:
                logger.warning("update_message: could not resolve the owner DM", exc_info=True)
        denied = not owner_dm or channel != owner_dm
        # Deliberately does NOT echo the resolved owner DM id: the refusal is
        # returned to the caller that just failed authorization.
        deny_reason = f"channel {channel} is not the owner's DM"
        deny_code = "not_owner_dm"
    else:
        denied = not is_tracked_channel(channel)
        deny_reason = f"channel {channel} not in tracked channels"
        deny_code = "channel_not_tracked"
    if denied:
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="update_message",
            tool_kind="slack",
            outcome="denied",
            downstream_service="slack",
            resources=f"channel={channel}",
        )
        return web.json_response({"error": deny_reason, "code": deny_code}, status=403)
    # Sanitize LLM-generated content before it reaches Slack, on the same
    # DISPLAY-form floor api_send_message uses: the literal-form scan alone lets a
    # markdown-collapse credential through, and an edit is posted as-is.
    text, _ = redact_for_display(text, _redact_all)
    if blocks:
        blocks = _sanitize_blocks(
            blocks, redact_exfiltration_urls, redact_credentials, display_form=True
        )
    slack = state.slack_client
    if not slack:
        return web.json_response(
            {"error": "Slack not connected", "code": "slack_not_connected"}, status=503
        )
    try:
        await slack.update_message(channel, ts, text, blocks)
    except Exception as e:
        safe_error = str(e).split("\n")[0][:200]
        safe_error, _ = redact_credentials(safe_error)
        safe_error, _ = redact_exfiltration_urls(safe_error)
        return web.json_response(
            {"error": f"Update failed: {safe_error}", "code": "update_failed"}, status=502
        )
    return web.json_response({"ok": True})


class _SendMessageBody(NamedTuple):
    """The fields of a ``POST /api/send-message`` body that ``_read_send_message`` admitted."""

    body: dict
    text: str
    title: Any
    blocks: Any
    target_channel: str
    target_user: str
    thread_ts: Any
    reply_broadcast: Any
    session_name: str
    channel_target: str
    channel_type: str


@dataclass
class _SendMessageOutcome:
    """What a send's fallback legs reached, recorded as each leg runs.

    The route's audit row reads it in a ``finally``, so a leg that raised still
    leaves the flags the legs before it set.
    """

    sent_slack: bool = False
    slack_ts: str | None = None
    slack_attempted: bool = False
    slack_error: str = ""
    sent_channel: bool = False
    channel_code: str = ""
    channel_detail: str = ""


async def _read_send_message(
    request: web.Request, state: "DashboardState"
) -> web.Response | _SendMessageBody:
    """Read a ``POST /api/send-message`` body, or the response that refuses it.

    The refusals run in the order the route has always run them. A body naming a
    configured channel target (``channel_type`` plus ``target_id``) is delivered
    here, because that leg answers before any Slack-shaped check.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    text = body.get("text", "").strip()
    if not text:
        return web.json_response({"error": "text required"}, status=400)
    title = body.get("title", "Agent Message")
    blocks = body.get("blocks")
    if blocks and not isinstance(blocks, list):
        return web.json_response({"error": "blocks must be an array"}, status=400)
    if isinstance(blocks, list) and _blocks_request_remote_media(blocks):
        return web.json_response(
            {
                "error": (
                    "agent-supplied Block Kit cannot contain image/video blocks "
                    "or image_url/thumbnail_url/video_url fields because Slack "
                    "fetches that media without a recipient click"
                ),
                "code": BLOCKS_REMOTE_MEDIA_ERROR,
            },
            status=400,
        )

    # ── Channel-addressed leg ──
    # Handled before the Slack-shaped validation below, because a Webex room id is
    # not a Slack channel id and must not have to satisfy CHANNEL_ID_RE. The target
    # is an OPAQUE ConfiguredChannelTarget id, never a raw platform conversation
    # id: this endpoint is reachable by the LLM, and re-resolving an opaque id
    # through the transport is what re-applies that channel's own allow-list at the
    # side-effect boundary.
    #
    # ``target_id`` is what selects THIS leg. ``channel_type`` alone means the
    # non-Slack conversation the SESSION already belongs to (``_deliver_to_channel``
    # further down); ``channel_type`` + ``target_id`` names an explicit configured
    # destination on that transport, which is this one. So the field pair reads as
    # "which transport, and — if given — which destination on it", and a
    # ``target_id`` with no transport to resolve it against is the only
    # under-specified combination.
    addressed_channel = str(body.get("channel_type", "") or "").strip().lower()
    target_id = str(body.get("target_id", "") or "").strip()
    if target_id:
        if not addressed_channel:
            return web.json_response(
                {
                    "error": "target_id requires channel_type",
                    "code": "channel_target_incomplete",
                },
                status=400,
            )
        # This leg is the explicit address, so it returns before the Slack-shaped
        # validation below ever runs. Refuse a Slack-only field or a routing
        # ``session`` travelling with it rather than dropping them: the caller
        # (a browser, or the model) cannot observe a drop, and would read a
        # private DM as a threaded post to a named Slack channel. Same posture,
        # and the same ``code`` shape, as the channel-session refusal further
        # down; ``presence`` not truthiness, because ``unfurl_links=False`` is
        # still a Slack option the caller asked for.
        stray = [f for f in _SLACK_ONLY_BODY_FIELDS if body.get(f) is not None]
        stray_session = body.get("session")
        if isinstance(stray_session, str) and stray_session:
            stray.append("session")
        if stray:
            return web.json_response(
                {
                    "error": (
                        "channel_type/target_id addresses the destination directly and "
                        f"cannot be combined with: {', '.join(stray)}"
                    ),
                    "code": "slack_field_with_channel_target",
                },
                status=400,
            )
        # Vet on the CALLER's REAL identity so the fail-closed ``channels`` re-vet
        # inside the leg resolves the caller's own profile rather than a
        # permissive ``HOST_SESSION_KEY`` default. Filtering to ``cron:`` here
        # discarded every non-cron caller's identity, and the MCP-side channel vet
        # fails OPEN on an evaluation error, so a non-cron session whose own
        # profile denies the transport could reach a host-permitted target.
        #
        # The identity is honoured ONLY on the proven-internal transport:
        # ``internal_auth`` is set solely after a constant-time ``X-Internal-Secret``
        # match — the path the MCP gateway and cron use, where ``caller_session``
        # is derived from the verified session key, not a tool arg. This route is
        # on ``_STRICT_INTERNAL_API_PATHS`` today (no browser reaches it), so the
        # gate also guards a future reclassification. Absent (a direct operator
        # send naming no session) degrades to the host sentinel inside the leg.
        addressed_caller = body.get("caller_session", "") if request.get("internal_auth") else ""
        return await _send_to_channel_target(
            state, addressed_channel, target_id, text, caller_session=addressed_caller
        )

    target_channel = body.get("channel", "").strip()
    target_user = body.get("user", "").strip()
    unfurl_links = body.get("unfurl_links")
    unfurl_media = body.get("unfurl_media")
    if (unfurl_links is not None and not isinstance(unfurl_links, bool)) or (
        unfurl_media is not None and not isinstance(unfurl_media, bool)
    ):
        return web.json_response(
            {"error": "unfurl_links and unfurl_media must be booleans"}, status=400
        )
    # Refused, not silently dropped (same posture as _SLACK_ONLY_BODY_FIELDS):
    # this endpoint is reachable from agent-authored tool calls, and a Slack
    # unfurl is a zero-click fetch of a possibly agent-written URL, so an
    # explicit ``true`` is the one bit a prompt-injected agent needs to
    # re-enable the exfiltration channel. ``false``/absent are accepted for
    # backward compatibility — they ask for what is now always the case.
    # See docs/request-for-change/rfc-redaction-explain-and-reveal.md §5.
    if unfurl_links or unfurl_media:
        return web.json_response(
            {
                "error": (
                    "unfurl_links/unfurl_media cannot be enabled: bot posts "
                    "never fetch link or media previews (a preview is a "
                    "zero-click request of a possibly agent-written URL)"
                ),
                "code": "unfurl_disabled",
            },
            status=400,
        )

    thread_ts = body.get("thread_ts")
    if thread_ts is not None:
        if not _is_slack_ts(thread_ts):
            return web.json_response(
                {"error": "thread_ts must be a Slack timestamp string like '1712793600.123456'"},
                status=400,
            )
    reply_broadcast = body.get("reply_broadcast")
    if reply_broadcast is not None and not isinstance(reply_broadcast, bool):
        return web.json_response({"error": "reply_broadcast must be a boolean"}, status=400)
    if reply_broadcast and not thread_ts:
        return web.json_response({"error": "reply_broadcast requires thread_ts"}, status=400)

    # Fail fast: mutual exclusion before any redaction/regex work (#4)
    if target_channel and target_user:
        return web.json_response({"error": "specify channel or user, not both"}, status=400)

    # A ``session`` naming a channel transport takes the routing over, so a
    # Slack-only option travelling with it has no destination. Refuse here,
    # before any delivery, rather than posting a message whose thread/layout/
    # audience request was silently discarded.
    raw_session = body.get("session")
    session_name = raw_session if isinstance(raw_session, str) else ""
    channel_target = (
        session_name
        if session_name not in _RESERVED_SESSION_TARGETS and _CHANNEL_TYPE_RE.match(session_name)
        else ""
    )
    if channel_target:
        slack_only = [f for f in _SLACK_ONLY_BODY_FIELDS if body.get(f) is not None]
        if slack_only:
            return web.json_response(
                {
                    "error": (
                        f"session '{channel_target}' does not accept the Slack-only "
                        f"field(s): {', '.join(slack_only)}"
                    ),
                    "code": "slack_only_field_with_channel_session",
                },
                status=400,
            )

    channel_type = body.get("channel_type") or ""
    if not isinstance(channel_type, str):
        return web.json_response(
            {"error": "channel_type must be a string", "code": "channel_type_not_a_string"},
            status=400,
        )
    channel_type = channel_type.strip()
    if channel_type:
        # Refused, never resolved by precedence: with two destinations named,
        # either order silently drops one and the caller cannot tell which. The
        # field list is the shared ``_SLACK_ONLY_BODY_FIELDS`` rather than a
        # hand-rolled three, so a Slack option added there is refused here too.
        conflicts = [f for f in _SLACK_ONLY_BODY_FIELDS if body.get(f) is not None]
        if session_name == SLACK_NAMESPACE:
            conflicts.append('session="slack"')
        if conflicts:
            return web.json_response(
                {
                    "error": (
                        f"channel_type cannot be combined with {', '.join(conflicts)} — "
                        "those route to Slack only"
                    ),
                    "code": "channel_type_conflicts_slack_routing",
                },
                status=400,
            )
        if channel_type == SLACK_NAMESPACE:
            return web.json_response(
                {
                    "error": 'channel_type "slack" is not supported — use session="slack"',
                    "code": "channel_type_slack_unsupported",
                },
                status=400,
            )
        if channel_type not in _SEND_MESSAGE_CHANNEL_TYPES:
            return web.json_response(
                {
                    "error": (
                        f"unknown channel_type {channel_type!r} — expected one of "
                        f"{', '.join(sorted(_SEND_MESSAGE_CHANNEL_TYPES))}"
                    ),
                    "code": "channel_type_unknown",
                },
                status=400,
            )
    # Two DESTINATIONS named, so it is refused rather than resolved by branch
    # order: whichever won, the caller would be told the send succeeded to a place
    # they did not ask for. ``session="slack"`` is caught by the conflicts list
    # above for the same reason.
    #
    # ``session="origin"`` is deliberately NOT caught here, and is not a third
    # destination: it is a MODE meaning "inject where this came from", which is why
    # it sits in ``_RESERVED_SESSION_TARGETS`` rather than resolving to a channel.
    # Combined with channel_type it is the fallback ladder the cron path needs -- a
    # job whose origin slot has died still reaches its user on the channel -- and
    # the response reports ``delivered_to`` as the surface that actually took the
    # message, so a caller is never told the channel received something it did not.
    if channel_type and channel_target:
        return web.json_response(
            {
                "error": (
                    f"channel_type '{channel_type}' cannot be combined with "
                    f"session '{channel_target}' — each names a different destination"
                ),
                "code": "channel_type_conflicts_channel_session",
            },
            status=400,
        )
    return _SendMessageBody(
        body,
        text,
        title,
        blocks,
        target_channel,
        target_user,
        thread_ts,
        reply_broadcast,
        session_name,
        channel_target,
        channel_type,
    )


async def _deliver_send_message_fallback(
    state: "DashboardState",
    body: dict,
    outcome: _SendMessageOutcome,
    *,
    text: str,
    title: Any,
    blocks: Any,
    options: list[str],
    target_channel: str,
    target_user: str,
    thread_ts: Any,
    reply_broadcast: Any,
    target_session: str,
    job_name: str | None,
    channel_target: str,
    channel_type: str,
    caller_session: str,
    declared_session: str,
    is_cron_caller: bool,
    send_to_slack: bool,
) -> None:
    """Deliver a send no origin session took: the bell, then a channel or Slack.

    Each leg records what it reached on *outcome* as it goes.
    """
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls  # noqa: F811

    # Snapshot before the suffix below: that sentence describes the BELL's
    # delivery, and a channel post is a real delivery, not the
    # notification fallback it announces. A channel-born cron reaches
    # here with job_name set on every run (its job.session_key names a
    # channel session, never a dashboard slot), so this is the normal
    # path for one, not an edge case.
    channel_text = text
    if target_session and job_name:
        safe_name, _ = redact_exfiltration_urls(job_name)
        safe_name, _ = redact_credentials(safe_name)
        title = f"⏰ {safe_name}"
        text += "\n\n_(session closed — delivered as notification)_"
    state.notify("agent", title, text)
    # No widget on either channel path, so a parsed [OPTIONS:] trailer is
    # re-attached as a numbered list rather than dropped: the user still
    # learns the choices exist and can answer by typing one. Built from the
    # snapshot, so the notification-fallback sentence never rides along, and
    # hoisted above both legs so neither mechanism drops it.
    if options:
        channel_text = f"{channel_text}\n\n{format_overflow(options, 0)}"
    if channel_target:
        outcome.sent_channel, outcome.channel_code, outcome.channel_detail = (
            await _deliver_channel_dm(
                state,
                channel_target,
                channel_text,
                # Vet on the CALLER's real identity so the fail-closed
                # ``channels`` re-vet inside the leg resolves that caller's own
                # profile rather than the permissive ``HOST_SESSION_KEY``
                # default. Filtering to ``cron:`` here discarded every non-cron
                # caller's identity, and the MCP-side channel vet fails OPEN on
                # an evaluation error, so a non-cron session whose own profile
                # denies the transport could reach a host-permitted target --
                # the same hole the ``channel_type``/``target_id`` leg above
                # already closed, on the leg that was not migrated with it.
                #
                # The two arms take their identity from where each is trustworthy,
                # matching ``_channel_delivery_key`` on this same path: a cron's
                # body key is format-validated in ``api_send_message`` before it may
                # escalate routing, and a non-cron caller is identified by the
                # ``X-Session-Key`` header, which ``token_auth._verify_unix_peer``
                # kernel-attests against the peer's own process ancestry -- never
                # the body, which a tool arg could name. So the governance vet and
                # the delivery key resolve the SAME principal. Absent (a direct
                # operator send naming no session) still degrades to the host
                # sentinel inside the leg, unchanged.
                caller_session=caller_session if is_cron_caller else declared_session,
            )
        )
    if channel_type:
        outcome.sent_channel = await _deliver_to_channel(
            state,
            _channel_delivery_key(state, caller_session, declared_session),
            channel_text,
            channel_type=channel_type,
        )
    # Opt-in "Open session" deep-link button: resolved (server-side) only
    # when the caller asked for it AND this send actually reaches Slack.
    # Owner DM only -- not a named channel, not another user's DM: the link
    # carries a presigned auth token (see _resolve_session_link_url) that
    # must not leak into a shared channel, and only the owner can open the
    # dashboard anyway. That is exactly the no-target fall-through path
    # (``elif state.owner_id`` below), so gate it on the absence of both.
    # Built here, before the post, so the button rides the same Slack leg.
    session_link_url = ""
    # ``is True``, not truthiness: a string like "false" (e.g. from a
    # script caller serializing booleans) must not opt in to a
    # credential-bearing button. The schema validator coerces real
    # callers to a bool; anything else is treated as not-opted-in.
    if (
        body.get("include_session_link") is True
        and send_to_slack
        and state.slack_client
        and not target_channel
        and not target_user
    ):
        session_link_url = await _resolve_session_link_url(state, caller_session, declared_session)
    # A separate ``if``, not an ``elif``: ``send_to_slack`` is the single
    # predicate that decides Slack delivery, so it must be false when a
    # channel session took the routing over rather than merely
    # unreachable behind another branch.
    if send_to_slack and state.slack_client:
        await _post_send_message_to_slack(
            state,
            outcome,
            blocks=blocks,
            text=text,
            options=options,
            target_channel=target_channel,
            target_user=target_user,
            thread_ts=thread_ts,
            reply_broadcast=reply_broadcast,
            session_link_url=session_link_url,
        )


async def _post_send_message_to_slack(
    state: "DashboardState",
    outcome: _SendMessageOutcome,
    *,
    blocks: Any,
    text: str,
    options: list[str],
    target_channel: str,
    target_user: str,
    thread_ts: Any,
    reply_broadcast: Any,
    session_link_url: str,
) -> None:
    """Post a send to Slack: the named channel, the named user's DM, or the owner's."""
    try:
        if target_channel:
            channel = target_channel
        elif target_user:
            channel = await state.slack_client.open_dm(target_user)
        elif state.owner_id:
            channel = await state.slack_client.open_dm(state.owner_id)
        else:
            channel = ""

        if channel:
            outcome.slack_attempted = True
            # The opt-in "Open session" button rides the SAME Slack
            # message whenever that message can carry Block Kit blocks
            # -- caller-supplied blocks, or a plain-text send upgraded
            # to a text section -- so an opted-in send is ONE message
            # (one notification), not a message plus a bare-button
            # follow-up. Attaching the button never risks the caller's
            # message: a combined post Slack rejects falls back to the
            # message alone, with the button trailing as its own
            # best-effort follow-up (see below).
            link_blocks = _session_link_blocks(session_link_url) if session_link_url else []
            # Deferred import, matching the messaging handlers' other slack_sdk
            # uses. SlackApiError is the ONE failure shape where Slack
            # ANSWERED (ok=false): the combined post definitively did
            # not land, so retrying without the button cannot deliver
            # the caller's message twice. Every other exception
            # (timeout, connection drop) is ambiguous -- the post may
            # have landed -- so those propagate to the delivery-failed
            # path below instead of triggering a duplicate-risking
            # second post.
            from slack_sdk.errors import SlackApiError

            button_rode_primary = False
            if blocks:
                # Caller Block Kit blocks: append the button to them.
                try:
                    outcome.slack_ts = await state.slack_client.post_blocks(
                        channel,
                        blocks + link_blocks,
                        text,
                        thread_ts=thread_ts,
                        reply_broadcast=reply_broadcast,
                    )
                    button_rode_primary = bool(link_blocks)
                except SlackApiError:
                    if not link_blocks:
                        raise
                    # Slack REJECTED the combined post; the caller's
                    # blocks must still post, so retry them alone and
                    # let the button trail as a follow-up.
                    outcome.slack_ts = await state.slack_client.post_blocks(
                        channel,
                        blocks,
                        text,
                        thread_ts=thread_ts,
                        reply_broadcast=reply_broadcast,
                    )
            elif link_blocks and not options and 0 < len(text) <= _SLACK_SECTION_TEXT_MAX:
                # Plain-text send WITH a link: one message carrying a
                # text section plus the button. Falls back to text-only
                # (button trails) only when Slack REJECTS the combined
                # post (SlackApiError = answered ok=false, nothing
                # landed); an ambiguous transport failure propagates
                # rather than risking the text posting twice.
                try:
                    outcome.slack_ts = await state.slack_client.post_blocks(
                        channel,
                        [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
                        + link_blocks,
                        text,
                        thread_ts=thread_ts,
                        reply_broadcast=reply_broadcast,
                    )
                    button_rode_primary = True
                except SlackApiError:
                    outcome.slack_ts = await state.slack_client.post_message(
                        channel,
                        text,
                        thread_ts=thread_ts,
                        reply_broadcast=reply_broadcast,
                    )
            else:
                outcome.slack_ts = await state.slack_client.post_message(
                    channel,
                    text,
                    thread_ts=thread_ts,
                    reply_broadcast=reply_broadcast,
                )
                if options:
                    try:
                        # Asker is the thread's owner: an out-of-band
                        # post has no running session of its own, so
                        # the conversation that receives the reply is
                        # the right subject.
                        _sm_o = slack_options_owner_key(state, thread_ts or "")
                        _sm_t = (
                            await asyncio.to_thread(mint_options_token, state, _sm_o)
                            if _sm_o
                            else None
                        )
                        option_blocks = build_options_blocks(options, staleness_token=_sm_t)
                        # Fallback text is the SAFE stub, not the
                        # message body. Slack parses entities in a
                        # message's top-level `text` -- which is what
                        # notifications render -- so an agent-authored
                        # body containing `<!channel>` would ping the
                        # whole channel, and the expiry would ping it
                        # AGAIN every time it replays this text on its
                        # edit. Nothing is lost: the body was already
                        # posted as its own message just above, so here
                        # it was pure duplication. This is the same stub
                        # the other three posting paths use.
                        option_ts = await state.slack_client.post_blocks(
                            channel,
                            option_blocks,
                            OPTIONS_FALLBACK_TEXT,
                            thread_ts=thread_ts,
                        )
                        # A thread IS a conversation, so bind the
                        # control to whichever session owns that
                        # thread — a dashboard session mirroring into
                        # it, or the Slack-born one. Without a thread
                        # there is no conversation to supersede it, so
                        # nothing is recorded.
                        if thread_ts and option_ts:
                            remember_slack_options(
                                state,
                                slack_options_owner_key(state, str(thread_ts)),
                                PostedOptions(
                                    channel=channel,
                                    ts=option_ts,
                                    choices=tuple(options),
                                    blocks=tuple(option_blocks),
                                ),
                            )
                    except Exception:
                        logger.debug(
                            "send_message: failed to post OPTIONS blocks",
                            exc_info=True,
                        )
            outcome.sent_slack = True
            # Trailing "Open session" button -- posted as its own
            # message ONLY when it could not ride the primary one (a
            # caller using `options`, or a combined post Slack
            # rejected). Best-effort and isolated: a link Slack rejects
            # fails only this follow-up, never the message the caller
            # actually sent (already delivered above). Threaded with
            # the main post when that was a threaded reply.
            if session_link_url and not button_rode_primary:
                try:
                    await state.slack_client.post_blocks(
                        channel,
                        _session_link_blocks(session_link_url),
                        "Open session",
                        thread_ts=thread_ts,
                    )
                except Exception:
                    logger.debug(
                        "send_message: failed to post session-link button",
                        exc_info=True,
                    )
    except Exception as exc:
        outcome.slack_attempted = True
        outcome.slack_error = str(exc)
        logger.exception("send_message: Slack delivery failed")


def _audit_send_message(
    outcome: _SendMessageOutcome,
    *,
    sent_session: bool,
    target_channel: str,
    target_user: str,
    thread_ts: Any,
    reply_broadcast: Any,
    channel_target: str,
    channel_type: str,
) -> None:
    """Write the ``send_message`` SEL row: the surface that took the message, or the failure.

    Runs in the route's ``finally``; a row that cannot be written is logged, never raised.
    """
    try:
        thread_hint = " threaded=1" if thread_ts else ""
        if reply_broadcast:
            thread_hint += " broadcast=1"
        if target_channel or target_user:
            base_res = f"target_channel={target_channel} target_user={target_user}"
        elif sent_session:
            base_res = "session=origin"
        elif channel_target or channel_type:
            base_res = f"channel_type={channel_target or channel_type}"
        else:
            base_res = "fallback=owner_dm"
        if sent_session:
            downstream_service = "session"
        elif outcome.sent_channel:
            # The channel type itself, so a reader of the audit learns WHICH
            # surface took the message and a channel added later needs no new
            # vocabulary here.
            downstream_service = channel_target or channel_type
        elif outcome.sent_slack:
            downstream_service = "slack"
        else:
            downstream_service = "dashboard"
        # A refused or failed channel delivery is an error for the same reason a
        # failed Slack post is: the caller asked for that surface. channel_code is
        # empty when the channel was merely absent, which is the documented
        # degradation to a notification rather than a failure. The channel_type leg
        # has no soft miss, so any non-delivery is its 502 -- but a satisfied
        # session injection is not one, which is the same condition that guard
        # uses, so this row and the HTTP status cannot disagree.
        failed = (
            (outcome.slack_attempted and not outcome.sent_slack)
            or bool(outcome.channel_code)
            or bool(channel_type and not outcome.sent_channel and not sent_session)
        )
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="send_message",
            outcome="error" if failed else "completed",
            downstream_service=downstream_service,
            resources=base_res + thread_hint,
            error=outcome.channel_code,
        )
    except Exception:
        logger.warning("SEL logging failed for send_message", exc_info=True)


def _send_message_response(
    outcome: _SendMessageOutcome,
    *,
    sent_session: bool,
    channel_target: str,
    channel_type: str,
) -> web.Response:
    """The answer to a send whose legs have run: a failure, or where it was delivered."""
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls  # noqa: F811

    if outcome.channel_code:
        safe_detail, _ = redact_credentials(outcome.channel_detail)
        safe_detail, _ = redact_exfiltration_urls(safe_detail)
        detail = f"{channel_target} delivery failed: {safe_detail}"
        # Both responses are spelled out inline, with a literal status and a
        # literal body, rather than sharing a hoisted dict or computing the
        # status. `test_error_code_contract` ratchets BOTH of those shapes for the
        # same reason: a computed status and a body a static scan cannot read are
        # each a way to slip an uncoded error response past the gate, so the one
        # form that stays checkable is the verbose one. A governance refusal is
        # the caller's own permission problem; anything else is downstream.
        if outcome.channel_code == "channel_not_permitted":
            return web.json_response(
                {"ok": False, "error": detail, "code": outcome.channel_code}, status=403
            )
        return web.json_response(
            {"ok": False, "error": detail, "code": outcome.channel_code}, status=502
        )
    if outcome.slack_attempted and not outcome.sent_slack:
        safe_error, _ = redact_credentials(outcome.slack_error)
        safe_error, _ = redact_exfiltration_urls(safe_error)
        return web.json_response(
            {"ok": False, "error": f"Slack delivery failed: {safe_error}", "slack": False},
            status=502,
        )
    # A named channel that was not reached is a failure, not a notification-only
    # success: the caller asked for a specific conversation, Slack was suppressed
    # as its fallback, and the bell is not a substitute for the surface the user
    # is actually reading. _deliver_to_channel has already audited which of the
    # refusals it was.
    if channel_type and not outcome.sent_channel and not sent_session:
        return web.json_response(
            {
                "ok": False,
                "error": (
                    f"channel delivery to {channel_type} failed — the message was "
                    "not posted to the conversation"
                ),
                "code": "channel_delivery_failed",
            },
            status=502,
        )
    # Report the actual delivery channel so callers (and the read-back
    # steering) can distinguish a real Slack post from a notification-only
    # send; "ok: true" alone masks that difference.
    if sent_session:
        delivered_to = "session"
    elif outcome.sent_channel:
        # The channel type itself, so a caller reads WHICH surface took the message
        # and a channel added later needs no new vocabulary here.
        delivered_to = channel_target or channel_type
    elif outcome.sent_slack:
        delivered_to = "slack"
    else:
        delivered_to = "notification"
    resp_body: dict[str, Any] = {
        "ok": True,
        "slack": outcome.sent_slack,
        "session": sent_session,
        "delivered_to": delivered_to,
    }
    if outcome.slack_ts:
        resp_body["ts"] = outcome.slack_ts
    return web.json_response(resp_body)
