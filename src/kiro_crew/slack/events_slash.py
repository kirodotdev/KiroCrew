"""The ``/kirocrew`` slash command and its sub-commands.

``register_slash_command`` fills ``SLASH_REGISTRY`` at import with the handlers defined
here -- ``dashboard``, ``agent``, ``voice``, ``yolo``, ``config``, ``users``,
``channels``, ``sessions``, ``status`` and ``restart`` -- and ``_handle_slash`` routes an
incoming command to its handler (``_build_help_text`` lists them). ``_safe_log``
sanitizes user-controlled Slack text before it is logged, here and in ``_route_message``.

Split out of ``kiro_crew.slack.events``, which stays the import path and the patch
target: every name here is reached as ``kiro_crew.slack.events.<name>``, and a patch
through that facade reaches this module (``kiro_crew._facade_mirror``). Nothing here
imports the facade. New slash-command work goes here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import TYPE_CHECKING, Any, Callable, Coroutine

import aiohttp

from kiro_crew import slack_manifest
from kiro_crew.agent_discovery import agent_spec_stems
from kiro_crew.agent_spec_format import (
    is_markdown_spec,
    iter_agent_spec_files,
    parse_agent_spec_text,
)
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.dashboard.chat_utils import run_config_write
from kiro_crew.dashboard.token_auth import LINK_WINDOW_SECS, MAX_SESSION_TTL_SECS, parse_duration
from kiro_crew.executors import subprocess_executor
from kiro_crew.hooks import safe_read_file
from kiro_crew.platform import current_context
from kiro_crew.safety_override import safety_override, yolo_policy_permits
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.slack.allowlist import prompt_track_channel, send_dashboard_link
from kiro_crew.slack.blocks import channels_modal, dashboard_link_block, voice_config_modal
from kiro_crew.slack.events_queue import _spawn_tracked
from kiro_crew.slack.handler import describe_grant_lifetime, is_allowed_user, is_owner, slack_cfg
from kiro_crew.slack.sessions_view import (
    _build_sessions_blocks,
    _collect_recent_sessions_off_loop,
    _message_surface_limit,
    sessions_include_ended,
)
from kiro_crew.stats import Stats

if TYPE_CHECKING:
    from kiro_crew.slack.gateway import GatewayOrchestrator

logger = logging.getLogger("kiro_crew.slack.events")


# ---------------------------------------------------------------------------
# Slash command registry
# ---------------------------------------------------------------------------

# Handler signature: async def handler(orch, caller_id, args, respond) -> None
SlashHandler = Callable[["GatewayOrchestrator", str, str, Callable], Coroutine[Any, Any, None]]

SLASH_REGISTRY: dict[str, tuple[SlashHandler, str]] = {}


def register_slash_command(name: str, handler: SlashHandler, description: str = "") -> None:
    """Register a sub-command for ``/kirocrew <name>``."""
    SLASH_REGISTRY[name] = (handler, description)


def _build_help_text(cmd_name: str = "kirocrew") -> str:
    """Build help message listing all registered sub-commands."""
    lines = ["*Available commands:*"]
    for name, (_, desc) in sorted(SLASH_REGISTRY.items()):
        lines.append(f"• `/{cmd_name} {name}` — {desc}" if desc else f"• `/{cmd_name} {name}`")
    lines.append(f"• `/{cmd_name} #channel` — track/untrack channel")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Built-in slash sub-command handlers
# ---------------------------------------------------------------------------


async def _handle_dashboard(
    orch: GatewayOrchestrator, caller_id: str, args: str, respond: Callable
) -> None:
    """Generate presigned dashboard link and DM to caller."""
    ttl = 3600
    if args:
        parsed = parse_duration(args.split()[0])
        if parsed is None:
            await respond(f"Usage: `/{orch.slack_command} dashboard [<N>h|<N>m]`")
            return
        ttl = parsed

    session_ttl = min(ttl, MAX_SESSION_TTL_SECS)
    assert orch.slack is not None
    url = await send_dashboard_link(orch.slack, caller_id, session_ttl)
    if url:
        # Same clamp the mint applies (``exp = now + min(LINK_WINDOW_SECS,
        # session_ttl)``) and the same one the DM reports, so the ephemeral
        # block cannot outlast the link it describes.
        link_mins = min(LINK_WINDOW_SECS, session_ttl) // 60
        blks = dashboard_link_block(url, link_mins, session_ttl // 60)
        await respond("🔗 Dashboard link sent to your DMs.", blocks=blks)
    else:
        await respond("❌ Failed to send dashboard link.")


def _selector_agent_names() -> list[str]:
    """The sorted agent stems the ``/agent`` selector offers; a thread-side read."""
    agents_dir = kiro_agents_dir()
    if not agents_dir.is_dir():
        return []
    return sorted(agent_spec_stems(agents_dir, operation="slack_agent_selector", source="slack"))


async def _handle_agent(
    orch: GatewayOrchestrator, caller_id: str, args: str, respond: Callable
) -> None:
    """Switch agent directly if valid name given, otherwise show selector."""
    from kiro_crew.slack.handler import (  # circular import: handler.py imports events.py for command dispatch, creating runtime circular dependency
        _get_default_agent,
        _resolve_agent_name,
        _set_default_agent,
        is_owner,
    )

    if not is_owner(caller_id):
        await respond("⛔ Only the owner can switch agents.")
        return

    if args:
        name = args.strip().split()[0]
        if name.lower() in ("off", "default"):
            await run_config_write(_set_default_agent, "")
            await respond("🔄 Reset to default agent.")
            return
        # Resolution reads a spec's content; off the loop, like the listing.
        resolved = await asyncio.to_thread(_resolve_agent_name, name)
        if resolved:
            await run_config_write(_set_default_agent, resolved)
            await respond(f"🔄 Switched to agent: *{resolved}*")
            return
        await respond(f"❌ Unknown agent `{name}`. Pick one below:")

    # Show selector dropdown. The listing walks the agents directory and reads
    # every markdown candidate to decide whether it is a spec, so it runs off
    # the event loop: a large directory must not stall every other session.
    agent_names = await asyncio.to_thread(_selector_agent_names)
    current = _get_default_agent() or ""

    options = [{"text": {"type": "plain_text", "text": n[:75]}, "value": n} for n in agent_names]
    options.append({"text": {"type": "plain_text", "text": "off (default)"}, "value": "off"})
    initial = next((o for o in options if o["value"] == current), options[-1])

    blks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Current agent:* {current or 'default'}"},
            "accessory": {
                "type": "static_select",
                "action_id": "mc_agent_select",
                "options": options,
                "initial_option": initial,
            },
        },
    ]
    await respond("Select an agent:", blocks=blks)


async def _handle_voice(
    orch: GatewayOrchestrator, caller_id: str, args: str, respond: Callable
) -> None:
    """Open voice config modal with current TTS settings."""
    from kiro_crew.slack.handler import (
        _vc,  # circular import: handler.py imports events.py for command dispatch
    )

    trigger_id = getattr(orch, "_last_trigger_id", "")
    if not trigger_id:
        await respond("❌ Missing trigger_id — cannot open modal.")
        return

    modal = voice_config_modal(
        tts_enabled=_vc.global_enabled,
        auto_speak=getattr(_vc, "auto_speak", False),
        voice=_vc.default_voice,
        engine=_vc.default_engine,
        speed=_vc.default_rate,
        pitch=_vc.default_pitch,
        aws_profile=_vc.aws_profile,
        region=_vc.region,
    )

    try:
        assert orch.slack is not None
        await orch.slack.views_open(trigger_id=trigger_id, view=modal)
    except Exception:
        logger.exception("Failed to open voice config modal")
        await respond("❌ Failed to open voice settings modal.")


register_slash_command("dashboard", _handle_dashboard, "get a dashboard access link")
register_slash_command("agent", _handle_agent, "switch the active agent")
register_slash_command("voice", _handle_voice, "configure TTS voice settings")


async def _handle_yolo(
    orch: GatewayOrchestrator, caller_id: str, args: str, respond: Callable
) -> None:
    """Toggle YOLO mode on/off/renew."""
    if not is_owner(caller_id):
        await respond("⛔ Only the owner can toggle YOLO mode.")
        return

    arg = args.strip().lower()
    so = safety_override()

    if arg == "on":
        if so.is_active():
            await respond(f"🟢 YOLO mode is already *ON* ({describe_grant_lifetime()}).")
            return
        # Off-loop: activate() writes a SEL event and consults the
        # ``approval_modes`` policy, so running it inline stalls the whole gateway
        # on a slow home. The sibling slash path in handler.py already offloads it.
        result = await asyncio.to_thread(so.activate, "slack")
        if not result.active:
            # Arming can be REFUSED by policy, not just fail on audit. Reporting an
            # audit fault for a policy denial sends the owner to the wrong place, so
            # the two causes are told apart and they have two different places to
            # look: the org's policy, or the audit system. Same split as the slash
            # path in ``handler.py``, which must not drift from this one. The verdict
            # is a memory read (pushed at ceiling install), so no thread.
            if not yolo_policy_permits():
                await respond("🔒 YOLO mode is disabled by your organization's policy.")
            else:
                await respond("❌ Failed to activate YOLO mode (audit system unavailable).")
            return
        sel().log_api_access(
            caller=caller_id,
            operation="slack.yolo_mode",
            outcome="allowed",
            source="slack",
            resources="yolo_on",
        )
        if orch.dashboard_state:
            orch.dashboard_state.push_slots_update()
        await respond(
            f"🟢 YOLO mode *ON* ({describe_grant_lifetime()})" f" — all tools auto-approved."
        )
    elif arg == "off":
        from kiro_crew.slack.handler import (
            disable_yolo,  # circular import: handler.py imports events.py for command dispatch registration
        )

        disable_yolo()
        sel().log_api_access(
            caller=caller_id,
            operation="slack.yolo_mode",
            outcome="allowed",
            source="slack",
            resources="yolo_off",
        )
        if orch.dashboard_state:
            orch.dashboard_state.push_slots_update()
        await respond("🔴 YOLO mode *OFF* — tools require approval.")
    elif arg == "renew":
        # renew() audits fail-closed with a synchronous SEL write; keep that
        # filesystem I/O off the event loop.
        renew_result = await asyncio.to_thread(so.renew, "slack")
        if renew_result.renewed:
            sel().log_api_access(
                caller=caller_id,
                operation="slack.yolo_mode",
                outcome="renewed",
                source="slack",
                resources="yolo_renew",
            )
            if orch.dashboard_state:
                orch.dashboard_state.push_slots_update()
            await respond(f"🟢 YOLO mode *renewed* (auto-expires in {renew_result.ttl // 60}min).")
        else:
            await respond("🔴 YOLO mode is not active. Use `on` to activate first.")
    else:
        if so.is_active():
            await respond(
                f"YOLO mode is currently *ON 🟢* ({describe_grant_lifetime()}).\nUsage: `/{orch.slack_command} yolo on|off|renew`"
            )
        else:
            await respond(
                f"YOLO mode is currently *OFF 🔴*.\nUsage: `/{orch.slack_command} yolo on|off|renew`"
            )


async def _handle_config(
    orch: GatewayOrchestrator, caller_id: str, args: str, respond: Callable
) -> None:
    """Open config modal (owner-only) — users and channels."""
    if not is_owner(caller_id):
        await respond("⛔ Only the owner can change config.")
        return

    tracking_ids = list(orch._tracking_channels)

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "⚠️ Multi-user access is disabled for security. Only the owner can interact via Slack.",
            },
        },
        {
            "type": "input",
            "block_id": "channels_block",
            "label": {"type": "plain_text", "text": "Tracked Channels"},
            "element": {
                "type": "multi_channels_select",
                "action_id": "mc_config_channels",
                "placeholder": {"type": "plain_text", "text": "Select channels"},
                **({"initial_channels": tracking_ids} if tracking_ids else {}),
            },
            "optional": True,
        },
    ]

    view = {
        "type": "modal",
        "callback_id": "mc_config_panel",
        "title": {"type": "plain_text", "text": "Kiro Crew Config"},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }

    trigger_id = getattr(orch, "_last_trigger_id", "")
    if not trigger_id:
        await respond("⚠️ Cannot open modal — missing trigger_id.")
        return

    try:
        assert orch.slack is not None
        await orch.slack.views_open(trigger_id=trigger_id, view=view)
    except Exception:
        logger.exception("Failed to open config modal")
        await respond("❌ Failed to open config modal.")


register_slash_command("yolo", _handle_yolo, "toggle YOLO mode (auto-approve tools)")
register_slash_command("config", _handle_config, "manage users and channels (owner-only)")


async def _handle_allowlist_cmd(
    orch: GatewayOrchestrator, caller_id: str, args: str, respond: Callable
) -> None:
    """Multi-user access disabled — user management is blocked."""
    await respond(
        "⛔ Multi-user access is disabled for security. Only the owner can use Kiro Crew via Slack."
    )


def _get_agent_names() -> list[str]:
    """Return sorted list of installed agent names from ~/.kiro/agents/.

    Reads each agent JSON's ``name`` field via ``hooks.safe_read_file`` so
    symlinks into sensitive paths (e.g. ``~/.aws/credentials``) are blocked
    by ``is_sensitive_path()``. Falls back to the filename stem when the
    file cannot be read safely or the JSON does not carry a usable name.

    When a read is blocked by ``is_sensitive_path()``, a SEL audit event
    (``sensitive_path_blocked``) is emitted so the attempt is observable.
    """
    agents_dir = kiro_agents_dir()
    if not agents_dir.is_dir():
        return []
    names = []
    for f in iter_agent_spec_files(agents_dir, ordered=False):
        try:
            data = parse_agent_spec_text(safe_read_file(str(f)), f)
            name = data.get("name") if isinstance(data, dict) else None
        except PermissionError as exc:
            # Symlink or resolved path landed in a sensitive location — audit it.
            try:
                sel().log_api_access(
                    caller="system",
                    operation="sensitive_path_blocked",
                    outcome="denied",
                    source="slack.events._get_agent_names",
                    resources=str(f),
                    error=str(exc),
                )
            except Exception:
                logger.debug(
                    "Failed to emit SEL audit event for blocked agent read: %s",
                    f,
                    exc_info=True,
                )
            name = None
        except (ValueError, OSError):
            if is_markdown_spec(f):
                # A markdown file that does not parse as a spec is not a spec
                # (a README, notes); only a broken JSON keeps its stem below.
                continue
            # ValueError covers bad JSON, bad frontmatter AND UnicodeDecodeError
            # (a ValueError subclass, NOT an OSError), which is
            # raised by safe_read_file's utf-8 read on a non-UTF-8 *.json —
            # e.g. a macOS AppleDouble ._foo.json stub in ~/.kiro/agents.
            # Catching it here keeps a non-UTF-8 file from crashing the
            # `/kirocrew channels` handler / channel-modal refresh task before
            # it opens. Mirrors agent_parts/spec_build.py's _load_json.
            name = None
        names.append(name or f.stem)
    return sorted(names)


async def _handle_channel_cmd(
    orch: GatewayOrchestrator, caller_id: str, args: str, respond: Callable
) -> None:
    """Open modal showing tracked channels with per-channel activation mode."""
    if not is_owner(caller_id):
        await respond("⛔ Only the owner can manage tracked channels.")
        return

    current_ids = sorted(orch._tracking_channels)
    channels = [
        {
            "channel_id": cid,
            "activation": slack_cfg(orch).channel_config(cid).activation,
            "agent": slack_cfg(orch).channel_config(cid).agent,
        }
        for cid in current_ids
    ]
    # Reads every spec's declared name; off the loop like the other listings.
    agent_names = await asyncio.to_thread(_get_agent_names)
    modal = channels_modal(channels, agent_names=agent_names)

    trigger_id = getattr(orch, "_last_trigger_id", "")
    if not trigger_id:
        await respond("⚠️ Cannot open modal — missing trigger_id.")
        return
    try:
        assert orch.slack is not None
        await orch.slack.views_open(trigger_id=trigger_id, view=modal)
    except Exception:
        logger.exception("Failed to open channels modal")
        await respond("❌ Failed to open channels modal.")


register_slash_command("users", _handle_allowlist_cmd, "manage allowed users")
register_slash_command("channels", _handle_channel_cmd, "manage tracked channels")


async def _handle_sessions(
    orch: GatewayOrchestrator, caller_id: str, args: str, respond: Callable
) -> None:
    """List last 10 sessions as task_card blocks with resume buttons.

    *args* of ``all`` or ``ended`` includes rows the user has dismissed with
    End; by default those are left out, so End takes a row off the list.
    """

    # Deny-by-default authorization gate (defense-in-depth).
    #
    # Session JSONLs contain prior conversation contents — only owner /
    # explicitly-allowed users may read them. The dispatcher in
    # ``_handle_slash`` already filters slash commands by allowlist, so in
    # production this branch is unreachable today. The check is still
    # required by the security-controls rule (deny-by-default) and
    # protects against future refactors that bypass the dispatcher gate.
    # The ``sessions`` keyword path applies the same check in handler_runtime/dispatch.py
    # before delegating to ``_handle_sessions_command``.
    if not (is_owner(caller_id) or is_allowed_user(caller_id)):
        sel().log_api_access(
            caller=caller_id,
            operation="slack.sessions_slash_data_access",
            outcome="denied",
            source="slack",
            resources=args or "",
            error="unauthorized caller",
        )
        await respond("_Permission denied._")
        return

    # Wrap the collector so a transient OSError still produces a SEL audit
    # entry. Without this, an IO failure would skip the audit entirely and
    # the access attempt would be invisible to the security pipeline. The
    # Home Tab error-path (in ``_publish_home_tab``) follows the same
    # pattern: capture the exception, redact-then-truncate the message,
    # and emit an ``error=`` audit field.
    try:
        rows = await _collect_recent_sessions_off_loop(
            orch.sessions if orch is not None else None,
            limit=_message_surface_limit(slack_cfg(orch).slack.sessions_limit),
            include_ended=sessions_include_ended(args or ""),
        )
    except Exception as exc:
        # Redact-then-truncate: redact() first so credential / exfil
        # patterns aren't split mid-string by the truncation step. The
        # SEL on-disk file is not internally redacted (sel.py only
        # redacts on forward), so this is defense-in-depth for the
        # security-controls "never trust output" rule applied
        # to exception messages from third-party libraries.
        redacted_exc, _ = redact_exfiltration_urls(str(exc))
        redacted_exc, _ = redact_credentials(redacted_exc)
        sel().log_api_access(
            caller=caller_id,
            operation="slack.sessions_slash_data_access",
            outcome="error",
            source="slack",
            resources="0 sessions read (collector failed)",
            error=redacted_exc[:200],
        )
        logger.exception("slash sessions: collector failed for caller %s", caller_id)
        await respond("_Sessions unavailable._")
        return

    sel().log_api_access(
        caller=caller_id,
        operation="slack.sessions_slash_data_access",
        outcome="allowed",
        source="slack",
        resources=f"{len(rows)} sessions read",
    )

    if not rows:
        await respond("_No recent sessions._")
        return

    blocks = _build_sessions_blocks(rows)
    await respond("\U0001f4cb Recent sessions:", blocks=blocks)


register_slash_command("sessions", _handle_sessions, "list recent sessions")


async def _handle_status(
    orch: GatewayOrchestrator, caller_id: str, args: str, respond: Callable
) -> None:
    """Show runtime stats summary."""
    # Identity status via the active PlatformContext (Default == OSS no-op stub
    # returning ""; an enterprise companion returns the real SSO status line).
    sso_line = await current_context().identity.status_line(prefix=" · sso")
    await respond(Stats().summary() + sso_line)


register_slash_command("status", _handle_status, "show runtime stats")


async def _handle_restart(
    orch: GatewayOrchestrator, caller_id: str, args: str, respond: Callable
) -> None:
    """Restart the gateway process (owner-only, requires systemd supervisor)."""
    if not is_owner(caller_id):
        sel().log_tool_invocation(
            session_key="",
            source="slack",
            tool_name="/kirocrew restart",
            outcome="denied",
            resources=f"user={caller_id}",
        )
        await respond("⛔ Only the owner can restart the gateway.")
        return

    if not os.environ.get("INVOCATION_ID"):
        sel().log_tool_invocation(
            session_key="",
            source="slack",
            tool_name="/kirocrew restart",
            outcome="denied",
            resources=f"user={caller_id},reason=no_supervisor",
        )
        await respond(
            "⛔ Restart requires a process supervisor (systemd). "
            "Running in bare mode — restart manually."
        )
        return

    sel().log_tool_invocation(
        session_key="",
        source="slack",
        tool_name="/kirocrew restart",
        outcome="approved",
        resources=f"user={caller_id}",
    )
    try:
        await respond("♻️ Restarting gateway…")
    except Exception:
        logger.debug("Restart notification failed", exc_info=True)
    try:
        if orch.dashboard_state and hasattr(orch.dashboard_state, "push_update_progress"):
            # circular import: dashboard.chat imports events via orchestrator
            # setup; events imports dashboard.chat for slot persistence
            from kiro_crew.dashboard.chat import save_all_slots_to_history

            orch.dashboard_state.push_update_progress("restarting", "Restarting server…")
            # save_all_slots_to_history does synchronous per-slot file I/O that can
            # block on a wedged disk; offload it to the dedicated subprocess_executor
            # (the pool reserved for potentially-hanging teardown work) bounded by
            # wait_for so it cannot freeze the event loop and prevent os._exit(1)
            # below. Using the default pool risks starving other default-pool
            # consumers if the thread wedges.
            await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    subprocess_executor(), save_all_slots_to_history, orch.dashboard_state
                ),
                timeout=5.0,
            )
    except Exception:
        logger.debug("Dashboard state save before restart failed", exc_info=True)
    try:
        if orch.sessions:
            # Bound the cleanup: a session close can hang waiting on a remote
            # peer, and os._exit() below would otherwise never be reached.
            # asyncio.TimeoutError subclasses Exception, so the handler proceeds
            # to exit on timeout.
            #
            # drain_timeout=2.0 keeps the pre-shutdown drain (internally bounded
            # to drain_timeout+1.0 = 3s) well inside this 5s outer deadline, so
            # the kill path still runs afterwards. close_all deliberately does
            # NOT catch CancelledError (propagates to keep this 5s deadline
            # honest); a still-held lock from a pathological overrun is recovered
            # by the orphan reaper on next startup.
            await asyncio.wait_for(orch.sessions.close_all(drain_timeout=2.0), timeout=5.0)
    except Exception:
        logger.debug("Session cleanup before restart failed", exc_info=True)
    # close_all() cut every dashboard turn that was mid-reply, and each one saves
    # the text its user watched stream as it unwinds, after the save above. The
    # exit below would beat that save. Bounded, so the exit is still reached.
    from kiro_crew.dashboard.chat_persistence import wait_for_unwinding_turns

    await wait_for_unwinding_turns(orch.dashboard_state)
    # The skill-usage ledgers hold up to one debounce window (60 s) of hits in
    # memory, and os._exit() below would drop them. Bounded and off-loop, for
    # the same reason as the SEL flush that follows.
    try:
        from kiro_crew import skill_usage

        await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), skill_usage.flush_all_for_shutdown
            ),
            timeout=skill_usage.SHUTDOWN_FLUSH_SECS,
        )
    except Exception:
        logger.debug("Skill-usage flush before restart failed", exc_info=True)
    # Flush the SEL audit queue: logging is async (background writer thread +
    # atexit flush) and os._exit() runs neither atexit handlers nor finalizers,
    # so the approved-restart audit event above would be lost. The shared
    # flush_audit_queue_before_hard_exit helper offloads the blocking drain to
    # the teardown pool under an outer deadline, so a wedged writer (disk full,
    # unreachable sink) cannot block the loop indefinitely and prevent
    # os._exit(1) from being reached. It drains only an existing singleton, so
    # it never creates a trust root on the way out.
    from kiro_crew.sel import flush_audit_queue_before_hard_exit

    await flush_audit_queue_before_hard_exit()
    # Same reason, same shape, for the OTHER async log sink: gateway.log runs
    # through a QueueListener thread, so its queued tail -- the restart
    # decision and everything logged during the teardown above -- dies with
    # the os._exit below unless it is drained first.
    from kiro_crew.cli import drain_log_queue_before_hard_exit

    await drain_log_queue_before_hard_exit()
    # hard_exit cancels an update apply in flight first (its build child
    # killed) rather than leaving it writing a tree after the exit.
    from kiro_crew import platform_compat

    platform_compat.hard_exit(1)


register_slash_command("restart", _handle_restart, "restart the gateway (owner-only)")


# Slash command handler
# ---------------------------------------------------------------------------


def _safe_log(text: str) -> str:
    """Sanitize free-form, user-controlled Slack text before logging.

    Strips CR/LF/tab to prevent log-forging (CWE-117) then redacts exfil URLs
    and credentials so customer prompt content isn't written verbatim to the
    app log (CWE-532). Mirrors the redaction the rest of this module already
    applies to other logged content.
    """
    if not text:
        return text
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


#: A command name the bundled manifest can register: ``/kirocrew`` or
#: ``/kirocrew-<alias>`` (``slack_manifest.slash_command``). Only these get the
#: mismatch reply. The alias class is what ``slash_command`` emits, which also
#: keeps the name safe to echo into the shell line the owner is asked to run.
_MANIFEST_COMMAND_RE = re.compile(
    rf"/(?:{re.escape(slack_manifest.DEFAULT_ALIAS)}"
    rf"|{re.escape(slack_manifest.SLASH_COMMAND_PREFIX)}[a-z0-9_-]+)"
)


async def _handle_slash(orch: GatewayOrchestrator, payload: dict) -> None:
    """Route ``/kirocrew <sub-command>`` via :data:`SLASH_REGISTRY`.

    Falls back to @user / #channel mention handling, then help text.
    """
    cmd = payload.get("command", "")
    cmd_text = payload.get("text", "").strip()
    caller_id = payload.get("user_id", "")
    response_url = payload.get("response_url", "")
    logger.info("Slash command: %s %s (caller=%s)", cmd, _safe_log(cmd_text), caller_id)

    def _spawn(coro: Coroutine[object, object, object]) -> asyncio.Task[object]:
        return _spawn_tracked(coro, owner=orch)

    async def _respond(text: str, blocks: list[dict] | None = None) -> None:
        if not response_url:
            return
        try:
            body: dict = {"text": text, "response_type": "ephemeral"}
            if blocks:
                body["blocks"] = blocks
            async with aiohttp.ClientSession() as sess:
                await sess.post(response_url, json=body)
        except Exception:
            logger.debug("slash response_url failed", exc_info=True)

    slash_command = f"/{orch.slack_command}"
    if cmd != slash_command:
        if _MANIFEST_COMMAND_RE.fullmatch(cmd):
            # Slack delivers only commands this app registered, so the app was
            # created or re-imported from a manifest that names the command
            # differently from slack.command: re-importing the bundled manifest
            # into an older install turns /kirocrew into /kirocrew-<alias>.
            fix = f"kirocrew config set slack.command {cmd[1:]}"
            logger.warning(
                "Slash command %s is not the configured %s; run `%s` and restart the gateway",
                cmd,
                slash_command,
                fix,
            )
            # Only the owner can make the fix, so only the owner is told.
            if is_allowed_user(caller_id):
                _spawn(
                    _respond(
                        f"⚠️ Kiro Crew is set up for `{slash_command}`, so `{cmd}` did "
                        f"nothing. To use `{cmd}`, run `{fix}` and restart the gateway."
                    )
                )
        return

    # Deny-by-default — only allowed users can invoke slash commands
    if not is_allowed_user(caller_id):
        sel().log_api_access(
            caller=caller_id,
            operation="slack.slash_command",
            outcome="denied",
            source="slack",
            resources=cmd_text,
            error="unauthorized sender",
        )
        _spawn(_respond("⛔ You are not authorized to use this command."))
        return

    sel().log_api_access(
        caller=caller_id,
        operation="slack.slash_command",
        outcome="allowed",
        source="slack",
        resources=cmd_text,
    )

    if not (orch.slack and orch._owner_id):
        _spawn(_respond("⚠️ Owner not configured."))
        return

    parts = cmd_text.split(maxsplit=1)
    sub_cmd = parts[0].lower() if parts else ""
    args = parts[1] if len(parts) > 1 else ""

    entry = SLASH_REGISTRY.get(sub_cmd)
    if entry is not None:
        handler, _ = entry
        # Stash trigger_id so modal-opening handlers can use it
        orch._last_trigger_id = payload.get("trigger_id", "")  # type: ignore[attr-defined]
        _spawn(handler(orch, caller_id, args, _respond))
        return

    # Fallback: @user mention — multi-user access disabled for security
    user_match = re.search(r"<@([A-Z0-9]+)(?:\|([^>]+))?>", cmd_text)
    if user_match:
        _spawn(
            _respond(
                "⛔ Multi-user access is disabled. Only the owner can use Kiro Crew via Slack."
            )
        )
        return

    # Fallback: #channel mention — Slack sends <#C1234|name> or <#C1234>
    channel_match = re.search(r"<#([A-Z0-9]+)(?:\|([^>]*))?>", cmd_text)
    if channel_match:
        channel_id = channel_match.group(1)
        channel_name = channel_match.group(2) or "Secret"
        _spawn(prompt_track_channel(orch.slack, orch._owner_id, channel_id, channel_name))
        _spawn(_respond(f"📨 Track request sent for #{channel_name or channel_id}."))
        return

    # Unknown sub-command → help
    _spawn(_respond(_build_help_text(orch.slack_command)))
