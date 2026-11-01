"""Slack modals: view submissions, the Forward to Agent shortcut and the settings modals.

``handle_view_submission`` and ``handle_view_closed`` dispatch a modal event to the
handler ``VIEW_REGISTRY`` holds for its ``callback_id``. ``_handle_message_shortcut``
opens the Forward to Agent modal and ``_handle_shortcut_submission`` sends the forwarded
text and file metadata to an agent session inside the untrusted-content fence that
``kiro_crew.slack.format`` builds for every forward path. The config panel
(``_handle_config_submission``) and voice config (``_handle_voice_config_submission``)
submissions register here.

Split out of ``kiro_crew.slack.interactions``, which stays the import path and the
patch target: every name here is reached as ``kiro_crew.slack.interactions.<name>``,
and a patch through that facade reaches this module (``kiro_crew._facade_mirror``).
Nothing here imports the facade. New modal work goes here.
"""

from __future__ import annotations

import asyncio
import json
import logging

from kiro_crew.config.loader import ConfigReadError, config_path, update_config_locked
from kiro_crew.dashboard.chat_utils import run_config_write
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.slack.format import (  # noqa: F401 -- re-bound for the facade
    _FENCE_MARKER_NEUTRALIZED,
    _FENCE_MARKER_RE,
    _FENCE_WORD_JOIN,
    ForwardedMessage,
    _extend_fence_span_left,
    _fence_fold,
    _neutralize_fence_markers,
    assemble_forward_message,
    count_forwarded_files,
    escape_mrkdwn,
    forwarded_files_from_slack,
)
from kiro_crew.slack.handler import (
    _SLACK_SECTION_TEXT_LIMIT,
    APPROVAL_INTERACTIVE,
    handle_message,
    is_allowed_user,
    is_owner,
    set_tracking_channels,
)
from kiro_crew.slack.interactions_context import (
    VIEW_REGISTRY,
    _orch,
    _probe_tracked_channel_scope,
    register_view_handler,
)
from kiro_crew.start_priority import StartPriority

logger = logging.getLogger("kiro_crew.slack.interactions")


async def handle_view_submission(payload: dict) -> None:
    """Dispatch a view_submission event to the registered handler."""
    view = payload.get("view", {})
    callback_id = view.get("callback_id", "")
    handler = VIEW_REGISTRY.get(callback_id)
    if handler is None and callback_id and callback_id == _get_forward_callback():
        # Live-reconfig fallback: the forward-to-agent callback is operator-
        # configurable, so unlike the fixed-string sibling handlers it may be
        # enabled/changed after init() ran (which registered nothing, or a now-
        # stale key). The modal-open path (_handle_message_shortcut) already
        # resolves the callback dynamically on every event; resolve it here too
        # so the open and submit paths agree and a forward can't silently vanish.
        handler = _handle_shortcut_submission
    if handler is None:
        logger.warning("No view handler registered for callback_id=%s", callback_id)
        return
    try:
        await handler(payload)  # type: ignore[misc]
    except Exception:
        logger.exception("View handler failed for callback_id=%s", callback_id)


async def handle_view_closed(payload: dict) -> None:
    """Dispatch a view_closed event. Uses same registry with ``_closed`` suffix fallback."""
    view = payload.get("view", {})
    callback_id = view.get("callback_id", "")
    # Try <callback_id>_closed first, then fall back to <callback_id>
    handler = VIEW_REGISTRY.get(callback_id + "_closed")
    if handler is None:
        logger.debug("No view_closed handler for callback_id=%s (ignored)", callback_id)
        return
    try:
        await handler(payload)  # type: ignore[misc]
    except Exception:
        logger.exception("View closed handler failed for callback_id=%s", callback_id)


# ---------------------------------------------------------------------------
# Config modal submission handler
# ---------------------------------------------------------------------------


async def _handle_config_submission(payload: dict) -> None:
    """Persist config modal changes to config.json and update runtime state."""
    caller = payload.get("user", {}).get("id", "")
    if not is_owner(caller):
        logger.warning("config_submission rejected: non-owner %s", caller)
        return
    view = payload.get("view", {})
    values = view.get("state", {}).get("values", {})

    # Parse allowlist — multi-user access disabled; ignore any stale allowlist_block
    # Parse tracked channels (multi_channels_select)
    chan_vals = values.get("channels_block", {}).get("mc_config_channels", {})
    new_channels = set(chan_vals.get("selected_channels") or [])

    # Persist through the locked read-modify-write BEFORE mutating runtime
    # state. Fail closed on an unreadable config: writing back a {} baseline
    # would drop every other setting the user has. Order matters — applying
    # the in-memory change first would make a refused save look like it took
    # effect, then silently revert on restart. The sidecar lock keeps a
    # concurrent config writer (dashboard PATCH, CLI, boot-time meta refresh)
    # from being reverted by this write's stale snapshot, and vice versa.
    cp = config_path()

    def _apply(data: dict) -> dict:
        slack_cfg = data.setdefault("slack", {})
        slack_cfg["tracking_channels"] = [{"channel_id": cid} for cid in sorted(new_channels)]
        return data

    try:
        await run_config_write(update_config_locked, cp, mutate=_apply)
    except ConfigReadError:
        logger.exception("Refusing to persist config from modal: config unreadable")
        return
    except OSError:
        logger.exception("Failed to persist config from modal")
        return

    if _orch:
        added = new_channels - _orch._tracking_channels
        _orch._tracking_channels = new_channels
        set_tracking_channels(new_channels)
        _probe_tracked_channel_scope(added)

    logger.info("Config updated via modal: channels=%d", len(new_channels))
    sel().log_api_access(
        caller=payload.get("user", {}).get("id", "unknown"),
        operation="slack.config_update",
        outcome="allowed",
        source="slack",
        resources=f"channels={len(new_channels)}",
    )


register_view_handler("mc_config_panel", _handle_config_submission)


# ---------------------------------------------------------------------------
# "Forward to Agent" message shortcut
# ---------------------------------------------------------------------------


def _get_forward_callback() -> str:
    """Return the configured forward-to-agent callback ID, or empty if disabled."""
    if not _orch:
        return ""
    from kiro_crew.slack.handler import slack_cfg

    return slack_cfg(_orch).slack.forward_to_agent_callback


#: Slack rejects modal views whose ``private_metadata`` exceeds 3000 characters.
_PRIVATE_METADATA_CAP = 3000
#: The shortcut carries at most this many file entries through ``private_metadata``.
_SHORTCUT_MAX_FILES = 5


def _fit_private_metadata(
    base: dict, text: str, files: list[dict], cap: int = _PRIVATE_METADATA_CAP
) -> str:
    """Serialize ``base`` + ``text`` + ``files`` as JSON within Slack's cap.

    JSON escaping makes the encoded length nonlinear in the text length (a CJK
    character encodes to six), so the text is cut to the LONGEST prefix whose
    encoding fits, found by binary search. Files are dropped only when even an
    empty text overflows, and each dropped one is added to
    ``base["omitted_files"]`` so the fence still says it existed.
    """
    base = {**base, "omitted_files": int(base.get("omitted_files") or 0)}

    def encode(body: str, entries: list[dict]) -> str:
        return json.dumps({**base, "text": body, "files": entries})

    while files and len(encode("", files)) > cap:
        files = files[:-1]
        base["omitted_files"] += 1
    if len(encode(text, files)) <= cap:
        return encode(text, files)
    if len(encode("", files)) > cap:
        return encode("", [])
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if len(encode(text[:mid], files)) <= cap:
            low = mid
        else:
            high = mid - 1
    return encode(text[:low], files)


async def _handle_message_shortcut(payload: dict) -> None:
    """Open a modal with the message text and an optional comment field."""
    expected = _get_forward_callback()
    if not expected:
        return
    callback_id = payload.get("callback_id", "")
    if callback_id != expected:
        logger.debug("Ignoring unknown message shortcut callback_id=%s", callback_id)
        return

    user_id = payload.get("user", {}).get("id", "")
    if not is_allowed_user(user_id):
        logger.warning("Message shortcut rejected: unauthorized user %s", user_id)
        sel().log_api_access(
            caller=user_id or "unknown",
            operation="slack.message_shortcut",
            outcome="denied",
            source="slack",
            error="unauthorized user",
        )
        return

    trigger_id = payload.get("trigger_id", "")
    if not trigger_id or not _orch or not _orch.slack:
        return

    msg = payload.get("message", {})
    msg_text = msg.get("text", "")[:3000]
    msg_text, _ = redact_exfiltration_urls(msg_text)
    msg_text, _ = redact_credentials(msg_text)
    msg_channel = payload.get("channel", {}).get("id", "")
    msg_ts = msg.get("ts", "")
    msg_user = msg.get("user", "")

    # Capture file METADATA (name/type/size/permalink, never content: the bot
    # token lacks files:read in trimmed production manifests) so a forwarded
    # image-only message reaches the agent as a described file instead of an
    # empty fence. Cap the count and name length so the metadata fits the
    # private_metadata budget alongside the text.
    norm_files = forwarded_files_from_slack(msg.get("files"))[:_SHORTCUT_MAX_FILES]
    omitted_files = max(0, count_forwarded_files(msg.get("files")) - len(norm_files))
    files_meta = [
        {
            "name": f.name[:120],
            "mimetype": f.mimetype,
            "filetype": f.filetype,
            "size": f.size,
            "permalink": f.permalink,
        }
        for f in norm_files
    ]

    # Carry the (already-redacted) message text plus file metadata in
    # private_metadata so the submission handler reads them back directly,
    # rather than reverse-parsing the modal's display blocks. Slack caps
    # private_metadata at 3000 chars; _fit_private_metadata trims the text
    # (then, as a last resort, trailing file entries) until the JSON fits.
    private = _fit_private_metadata(
        {"channel": msg_channel, "ts": msg_ts, "user": msg_user, "omitted_files": omitted_files},
        msg_text[:2500],
        files_meta,
    )

    # The modal shows exactly what the submission will carry: the fitted text
    # and file entries, plus a count of the dropped files. The section text must
    # stay inside Slack's section limit: build the file line first, then fit
    # the message preview into the room that is left.
    fitted = json.loads(private)
    fitted_files = fitted["files"]
    omitted_files = fitted["omitted_files"]
    display_prefix = f"*Message from* <@{msg_user}>:\n>>> "
    file_line = ""
    if fitted_files or omitted_files:
        display_names = [
            escape_mrkdwn(f["name"] or f["filetype"] or "file")[:120] for f in fitted_files
        ]
        total_files = len(fitted_files) + omitted_files
        file_line = f"\n📎 {total_files} file(s): {', '.join(display_names)}"
        if omitted_files:
            file_line += f" (+{omitted_files} more)"
    preview_text = fitted["text"]
    preview_room = max(0, _SLACK_SECTION_TEXT_LIMIT - len(display_prefix) - len(file_line))
    # An ellipsis marks a preview cut here or by the private_metadata fit.
    if len(preview_text) > preview_room or len(preview_text) < len(msg_text):
        preview = preview_text[: max(0, preview_room - 1)] + ("…" if preview_room else "")
    else:
        preview = preview_text
    display_text = f"{display_prefix}{preview}{file_line}"

    view = {
        "type": "modal",
        "callback_id": expected,
        "title": {"type": "plain_text", "text": "Forward to Agent"},
        "submit": {"type": "plain_text", "text": "Send"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "private_metadata": private,
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": display_text,
                },
            },
            {"type": "divider"},
            {
                "type": "input",
                "block_id": "comment_block",
                "optional": True,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "comment_input",
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Add your comment or question about this message…",
                    },
                },
                "label": {"type": "plain_text", "text": "Your comment"},
            },
        ],
    }

    try:
        await _orch.slack.views_open(trigger_id, view)
    except Exception:
        logger.exception("Failed to open message shortcut modal")
        sel().log_api_access(
            caller=user_id,
            operation="slack.message_shortcut",
            outcome="error",
            source="slack",
            resources=callback_id,
            error="views_open failed",
        )
        return

    sel().log_api_access(
        caller=user_id,
        operation="slack.message_shortcut",
        outcome="allowed",
        source="slack",
        resources=callback_id,
    )


async def _handle_shortcut_submission(payload: dict) -> None:
    """Process the 'Forward to Agent' modal submission."""
    user_id = payload.get("user", {}).get("id", "")
    if not is_allowed_user(user_id):
        sel().log_api_access(
            caller=user_id or "unknown",
            operation="slack.shortcut_submit",
            outcome="denied",
            source="slack",
            error="unauthorized user",
        )
        return
    if not _orch or not _orch.slack:
        return

    view = payload.get("view", {})
    values = view.get("state", {}).get("values", {})
    comment = (values.get("comment_block", {}).get("comment_input", {}).get("value") or "").strip()

    try:
        meta = json.loads(view.get("private_metadata", "{}"))
    except ValueError:
        meta = {}

    orig_channel = meta.get("channel", "")
    orig_ts = meta.get("ts", "")
    orig_user = meta.get("user", "")
    # The (already-redacted) message text and file metadata were stashed in
    # private_metadata at modal-open time, so read them straight back instead
    # of reverse-parsing the display blocks.
    orig_text = meta.get("text", "")
    orig_files = forwarded_files_from_slack(meta.get("files"))
    try:
        orig_omitted = max(0, int(meta.get("omitted_files") or 0))
    except (TypeError, ValueError, OverflowError):
        orig_omitted = 0

    # Build the text to send to the agent. The forwarded body (orig_text) is
    # authored by an arbitrary third party, possibly an external party in a
    # Slack-Connect/shared channel, and is NOT a trusted instruction source. It
    # goes through the shared assembler (also used by the native-forward event
    # path), which redacts it and fences it as untrusted, with one metadata line
    # per file so an image-only message produces a non-empty fence. The
    # submitting allowed user's own comment stays OUTSIDE the fence: it is
    # trusted first-party intent. Provenance is surfaced separately below through
    # the trusted ``action_context``, so ``include_provenance=False`` keeps it out
    # of the fence. The redaction below addresses data exfiltration on output;
    # the fence is the XPIA / prompt-injection guard on input.
    combined = assemble_forward_message(
        ForwardedMessage(
            text=orig_text,
            files=orig_files,
            author_id=orig_user,
            channel=orig_channel,
            ts=orig_ts,
            omitted_files=orig_omitted,
        ),
        comment=comment,
        include_provenance=False,
    )

    # Redact before routing
    combined, _ = redact_exfiltration_urls(combined)
    combined = redact_credentials(combined)[0]

    # Open/reuse DM with the submitting user
    try:
        dm_channel = await _orch.slack.open_dm(user_id)
    except Exception:
        logger.exception("Failed to open DM for shortcut submission")
        sel().log_api_access(
            caller=user_id,
            operation="slack.shortcut_submit",
            outcome="error",
            source="slack",
            error="open_dm failed",
        )
        return
    if not dm_channel:
        sel().log_api_access(
            caller=user_id,
            operation="slack.shortcut_submit",
            outcome="error",
            source="slack",
            error="open_dm failed",
        )
        return

    # Post the forwarded message as a visible user message in DM
    new_ts = await _orch.slack.post_message(dm_channel, combined)
    if not new_ts:
        logger.warning("Failed to post shortcut message to DM")
        sel().log_api_access(
            caller=user_id,
            operation="slack.shortcut_submit",
            outcome="error",
            source="slack",
            error="post_message failed",
        )
        return

    team_id = (payload.get("team") or {}).get("id", "")

    # Build context with origin info. The interpolated values are Slack IDs
    # (channel/ts/user), not free text, but neutralize each one for
    # defense-in-depth so a crafted ID can never forge the CONTEXT ENTRY
    # boundary. Neutralize the interpolated values ONLY — never the fence lines
    # themselves.
    context_parts = [
        f"channel={_neutralize_fence_markers(orig_channel)}",
        f"ts={_neutralize_fence_markers(orig_ts)}",
    ]
    if orig_user:
        context_parts.append(f"author=<@{_neutralize_fence_markers(orig_user)}>")
    action_context = (
        "--- CONTEXT ENTRY BEGIN ---\n"
        f"[Forwarded via message shortcut: {', '.join(context_parts)}]\n"
        "--- CONTEXT ENTRY END ---"
    )

    t = asyncio.create_task(
        handle_message(
            _orch.slack,
            _orch.sessions,  # type: ignore[arg-type]
            dm_channel,
            combined,
            new_ts,  # thread_ts — start a new thread from this message
            new_ts,
            user_id,
            team_id=team_id,
            approval_mode=APPROVAL_INTERACTIVE,
            context_builder=_orch.ctx_builder,
            cron_service=_orch.cron_svc,
            conversation_log=_orch.conv_log,
            consolidator=_orch.consolidator,
            subagent_manager=_orch.subagent_mgr,
            task_runner=_orch.task_runner,
            action_context=action_context,
            start_priority=StartPriority.FOREGROUND,
        )
    )
    _orch._handler_tasks.add(t)
    t.add_done_callback(_orch._handler_tasks.discard)

    sel().log_api_access(
        caller=user_id,
        operation="slack.shortcut_submit",
        outcome="allowed",
        source="slack",
        resources=f"from={orig_channel}:{orig_ts}",
    )


# ---------------------------------------------------------------------------
# Voice config view submission handler
# ---------------------------------------------------------------------------


async def _handle_voice_config_submission(payload: dict) -> None:
    """Save voice settings from mc_voice_config modal submission."""
    caller = payload.get("user", {}).get("id", "")
    if not is_owner(caller):
        return
    from kiro_crew.slack.handler import _vc

    values = payload.get("view", {}).get("state", {}).get("values", {})

    def _sel(block_id: str, action_id: str) -> str:
        opt = values.get(block_id, {}).get(action_id, {}).get("selected_option") or {}
        return opt.get("value", "")

    def _txt(block_id: str, action_id: str) -> str:
        return (values.get(block_id, {}).get(action_id, {}).get("value") or "").strip()

    # Compute the new values WITHOUT touching the live config yet. Fail closed
    # on an unreadable config: writing back a {} baseline would drop every
    # other setting. Order matters — mutating `_vc` first would let rejected
    # settings drive live TTS until the next restart, even though the save was
    # refused.
    cp = config_path()
    tts_block = values.get("tts_enabled_block", {}).get("mc_voice_tts_enabled", {})
    selected = {o.get("value") for o in tts_block.get("selected_options", [])}
    enabled = "enabled" in selected
    auto_speak = "auto_speak" in selected
    voice = _sel("voice_block", "mc_voice_voice") or _vc.default_voice
    engine = _sel("engine_block", "mc_voice_engine") or _vc.default_engine
    rate = _sel("speed_block", "mc_voice_speed") or _vc.default_rate
    pitch = _sel("pitch_block", "mc_voice_pitch") or _vc.default_pitch
    aws_profile = _txt("profile_block", "mc_voice_profile")
    region = _txt("region_block", "mc_voice_region")

    def _apply(data: dict) -> dict:
        vr = data.setdefault("voice_reply", {})
        vr["enabled"] = enabled
        vr["auto_speak"] = auto_speak
        vr["voice_id"] = voice
        vr["engine"] = engine
        vr["rate"] = rate
        vr["pitch"] = pitch
        vr["aws_profile"] = aws_profile
        vr["region"] = region
        return data

    # Persist FIRST (locked read-modify-write), then apply to the live config.
    # A failed write must not leave rejected settings driving live TTS until
    # the next restart.
    try:
        await run_config_write(update_config_locked, cp, mutate=_apply)
    except ConfigReadError:
        logger.exception("Refusing to persist voice settings: config unreadable")
        return
    except OSError:
        logger.exception("Failed to persist voice config from modal")
        return

    _vc.global_enabled = enabled
    _vc.auto_speak = auto_speak
    _vc.default_voice = voice
    _vc.default_engine = engine
    _vc.default_rate = rate
    _vc.default_pitch = pitch
    _vc.aws_profile = aws_profile
    _vc.region = region

    logger.info(
        "Voice config updated: enabled=%s voice=%s engine=%s speed=%s pitch=%s",
        enabled,
        voice,
        engine,
        rate,
        pitch,
    )


register_view_handler("mc_voice_config", _handle_voice_config_submission)
