"""Slack Socket Mode event routing.

Sets up the Socket Mode client, dispatches incoming events to the
correct handler:

- ``interactive`` → :mod:`interactions.dispatch`
- ``slash_commands`` → registry-based sub-command routing
- ``member_joined_channel`` → tracking-channel allowlist prompt
- ``app_home_opened`` → publish Home Tab view
- ``message`` / ``app_mention`` → :func:`handler.handle_message`

Also contains the bounded dedup cache (``_SeenCache``) that prevents
processing the same Slack event twice.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Callable, Coroutine

import aiohttp
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.socket_mode.websockets import SocketModeClient as WSSocketModeClient
from slack_sdk.web.async_client import AsyncWebClient

from kiro_crew import __version__
from kiro_crew.config.loader import (
    ACTIVATION_MENTION,
    ACTIVATION_OBSERVE,
    ACTIVATION_OFF,
    ACTIVATION_REVIEW,
    KiroCrewConfig,
)
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.cron import format_schedule
from kiro_crew.dashboard.handlers import get_update_info
from kiro_crew.dashboard.token_auth import LINK_WINDOW_SECS, MAX_SESSION_TTL_SECS, parse_duration
from kiro_crew.executors import subprocess_executor
from kiro_crew.hooks import safe_read_file
from kiro_crew.mcp_discovery import list_servers
from kiro_crew.messaging.identity import channel_inbound_permitted
from kiro_crew.platform import current_context, safe_context_call
from kiro_crew.platform.interfaces import InterceptDecision
from kiro_crew.safety_override import safety_override
from kiro_crew.security import (
    redact_credentials,
    redact_exfiltration_urls,
    should_record_observe_history,
)
from kiro_crew.sel import sel
from kiro_crew.skills import SkillsLoader
from kiro_crew.slack.allowlist import prompt_track_channel, send_dashboard_link
from kiro_crew.slack.blocks import (
    build_stopping_blocks,
    channels_modal,
    command_hint_block,
    dashboard_link_block,
    voice_config_modal,
)
from kiro_crew.slack.files import SLACK_AUDIO_MIMETYPES, process_slack_files
from kiro_crew.slack.handler import (
    APPROVAL_AUTO,
    APPROVAL_INTERACTIVE,
    describe_grant_lifetime,
    handle_message,
    is_allowed_user,
    is_owner,
    is_yolo_mode,
    set_allowed_users,
    set_dashboard_state,
    set_open_channels,
    set_orch_cfg,
    set_owner_id,
    set_tracking_channels,
    set_yolo_mode,
)
from kiro_crew.slack.interactions import dispatch as dispatch_interactive
from kiro_crew.slack.sessions_view import (
    _HOME_TAB_SESSIONS_PER_KIND,
    _SESSION_KIND_DASHBOARD,
    _SESSION_KIND_TASKRUNNER,
    _SESSIONS_DEFAULT_LIMIT,
    _build_sessions_blocks,
    _collect_recent_sessions,
)
from kiro_crew.slack.transport_dispatch import handle_message_transport
from kiro_crew.stats import Stats
from kiro_crew.transcribe import is_available as stt_available
from kiro_crew.transcribe import transcribe_audio

if TYPE_CHECKING:
    from kiro_crew.slack.client import SlackClientOps
    from kiro_crew.slack.gateway import GatewayOrchestrator

logger = logging.getLogger(__name__)

_skills_loader: SkillsLoader | None = None

# Strong references to fire-and-forget asyncio.Tasks scheduled from
# synchronous paths. Python's event loop keeps only *weak* references to
# tasks, so without a strong reference a task can be garbage-collected
# mid-execution. Tasks remove themselves on completion via add_done_callback.
_bg_tasks: set[asyncio.Task[object]] = set()


def _spawn_tracked(coro: Coroutine[object, object, object]) -> asyncio.Task[object]:
    """Schedule *coro* as a task and retain a strong reference until it finishes.

    ``asyncio.create_task``/``ensure_future`` alone is not enough: the event loop
    keeps only a weak reference, so a fire-and-forget task can be garbage-collected
    mid-execution (silently dropping the work). Tracking it in ``_bg_tasks`` and
    discarding on completion keeps it alive for its whole lifetime.
    """
    task = asyncio.ensure_future(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_on_tracked_done)
    return task


def _on_tracked_done(task: asyncio.Task[object]) -> None:
    """Discard a finished tracked task and surface any failure.

    A bare ``discard`` swallows exceptions from the spawned coroutine — a failed
    ``_respond`` POST (expired ``response_url``, network timeout) or a raising slash
    handler would store the exception in the task, which nobody reads and which is
    GC'd with the task, leaving operators blind to dropped slash replies. Log at
    DEBUG (these failures are routine and peer-driven) while still discarding.
    """
    _bg_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.debug("Tracked slash task failed: %s", exc)


def _get_skills_loader() -> SkillsLoader:
    global _skills_loader  # noqa: PLW0603
    if _skills_loader is None:
        _skills_loader = SkillsLoader()
    return _skills_loader


# Suppress noisy Slack SDK WebSocket reconnect errors — these are normal
# idle connection drops that the SDK handles automatically.
# WARNING lets ERROR through (recv failures, reconnect failures) while
# suppressing INFO (session established) and DEBUG (every message/ping).
logging.getLogger("slack_sdk.socket_mode.websockets").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Dedup cache — bounded LRU to avoid processing duplicate Slack events
# ---------------------------------------------------------------------------

_MAX_SEEN = 5000


# prevent GC of fire-and-forget tasks (Python event loop holds weak refs)
_background_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]


class SeenCache:
    """Bounded set that remembers the last *maxlen* event IDs."""

    def __init__(self, maxlen: int = _MAX_SEEN):
        self._d: OrderedDict[str, None] = OrderedDict()
        self._maxlen = maxlen

    def check_and_add(self, key: str) -> bool:
        """Return ``True`` if *key* was already seen, else mark it."""
        if key in self._d:
            return True
        self._d[key] = None
        if len(self._d) > self._maxlen:
            self._d.popitem(last=False)
        return False

    def check(self, key: str) -> bool:
        """Return ``True`` if *key* was already marked, WITHOUT marking it.

        Split from ``check_and_add`` for the message-interceptor dedup, which must
        record a key ONLY on a non-PROCESS (challenge/deny) decision — marking a
        PROCESS message would wrongly drop the paired ``app_mention`` event (same
        ``msg_ts``) or a standalone inline message. See ``_route_message``.
        """
        return key in self._d

    def add(self, key: str) -> None:
        """Mark *key* seen (idempotent, bounded)."""
        if key not in self._d:
            self._d[key] = None
            if len(self._d) > self._maxlen:
                self._d.popitem(last=False)


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
        blks = dashboard_link_block(url, LINK_WINDOW_SECS // 60, session_ttl // 60)
        await respond("🔗 Dashboard link sent to your DMs.", blocks=blks)
    else:
        await respond("❌ Failed to send dashboard link.")


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

    # Direct switch if arg provided
    if args:
        name = args.strip().split()[0]
        if name.lower() in ("off", "default"):
            _set_default_agent("")
            await respond("🔄 Reset to default agent.")
            return
        resolved = _resolve_agent_name(name)
        if resolved:
            _set_default_agent(resolved)
            await respond(f"🔄 Switched to agent: *{resolved}*")
            return
        await respond(f"❌ Unknown agent `{name}`. Pick one below:")

    # Show selector dropdown
    agents_dir = kiro_agents_dir()
    jsons = sorted(agents_dir.glob("*.json")) if agents_dir.is_dir() else []
    agent_names = sorted(f.stem for f in jsons)
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
        result = so.activate("slack")
        if not result.active:
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
            f"🟢 YOLO mode *ON* ({describe_grant_lifetime()})"
            f" — all tools auto-approved."
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
        renew_result = so.renew("slack")
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
    for f in agents_dir.glob("*.json"):
        try:
            data = json.loads(safe_read_file(str(f)))
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
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            # UnicodeDecodeError (a ValueError subclass, NOT an OSError) is
            # raised by safe_read_file's utf-8 read on a non-UTF-8 *.json —
            # e.g. a macOS AppleDouble ._foo.json stub in ~/.kiro/agents.
            # Catching it here keeps a non-UTF-8 file from crashing the
            # `/kirocrew channels` handler / channel-modal refresh task before
            # it opens. Mirrors agent.py's _load_json.
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
            "activation": orch._cfg.channel_config(cid).activation,
            "agent": orch._cfg.channel_config(cid).agent,
        }
        for cid in current_ids
    ]
    agent_names = _get_agent_names()
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
    """List last 10 sessions as task_card blocks with resume buttons."""
    # Deny-by-default authorization gate (defense-in-depth).
    #
    # Session JSONLs contain prior conversation contents — only owner /
    # explicitly-allowed users may read them. The dispatcher in
    # ``_handle_slash`` already filters slash commands by allowlist, so in
    # production this branch is unreachable today. The check is still
    # required by the security-controls rule (deny-by-default) and
    # protects against future refactors that bypass the dispatcher gate.
    # The ``sessions`` keyword path applies the same check at handler.py
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
        rows = _collect_recent_sessions(
            orch.sessions if orch is not None else None,
            limit=_SESSIONS_DEFAULT_LIMIT,
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
            session_key="", source="slack", tool_name="/kirocrew restart",
            outcome="denied", resources=f"user={caller_id}",
        )
        await respond("⛔ Only the owner can restart the gateway.")
        return

    if not os.environ.get("INVOCATION_ID"):
        sel().log_tool_invocation(
            session_key="", source="slack", tool_name="/kirocrew restart",
            outcome="denied", resources=f"user={caller_id},reason=no_supervisor",
        )
        await respond(
            "⛔ Restart requires a process supervisor (systemd). "
            "Running in bare mode — restart manually."
        )
        return

    sel().log_tool_invocation(
        session_key="", source="slack", tool_name="/kirocrew restart",
        outcome="approved", resources=f"user={caller_id}",
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
            await asyncio.wait_for(
                orch.sessions.close_all(drain_timeout=2.0), timeout=5.0
            )
    except Exception:
        logger.debug("Session cleanup before restart failed", exc_info=True)
    # Flush the SEL audit queue: logging is async (background writer thread +
    # atexit flush) and os._exit() runs neither atexit handlers nor finalizers,
    # so the approved-restart audit event above would be lost. flush() is a
    # synchronous blocking drain, so offload it to an executor bounded by
    # wait_for — a wedged writer (disk full, unreachable sink) must not block
    # the loop indefinitely and prevent os._exit(1) from being reached.
    try:
        await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(subprocess_executor(), sel().flush),
            timeout=3.0,
        )
    except Exception:
        logger.debug("SEL flush before restart failed", exc_info=True)
    os._exit(1)


register_slash_command("restart", _handle_restart, "restart the gateway (owner-only)")


# ---------------------------------------------------------------------------
# Socket Mode setup
# ---------------------------------------------------------------------------


def init_socket_mode(orch: GatewayOrchestrator, seen: SeenCache) -> None:
    """Wire up the Socket Mode client and attach the event listener.

    Does nothing when Slack is disabled (missing tokens or no allowed
    users).  Mutates ``orch._socket_client`` in place.
    """
    if not orch._slack_enabled:
        return

    if not orch._owner_id:
        logger.error("KIROCREW_OWNER_ID is not set — Slack disabled for security")
        orch._slack_enabled = False
        orch.slack = None
        return

    # Invariant: _allowed_users contains only the owner (multi-user disabled)
    assert orch._owner_id, "owner_id must be set"

    # Share owner-only allowlist and tracking channels with handler modules
    set_allowed_users(orch._allowed_users)
    set_tracking_channels(orch._tracking_channels)
    set_open_channels(orch._open_channels)
    set_owner_id(orch._owner_id)
    if orch._cfg.agent.dangerously_skip_permissions:
        set_yolo_mode(True)
    set_orch_cfg(orch._cfg)
    if orch.dashboard_state:
        set_dashboard_state(orch.dashboard_state)

    # ── Enterprise Grid workspace validation ──
    # Blocks data exfiltration via personal/external Slack workspaces.
    extra_ids = orch._cfg.slack_enterprise_ids
    # Route through the active PlatformContext's Slack enterprise gate.  The
    # Default gate is open (opt-in allowlist), identical to today; the Amazon
    # companion supplies a fail-closed workspace allowlist.
    if not current_context().slack_gate.validate_enterprise(orch._bot_token, extra_ids=extra_ids):
        logger.error("Slack workspace failed enterprise validation — Slack disabled")
        orch._slack_enabled = False
        orch.slack = None
        return

    web_client = AsyncWebClient(token=orch._bot_token)
    orch._socket_client = WSSocketModeClient(
        app_token=orch._app_token,
        web_client=web_client,
    )

    async def _on_event(client: WSSocketModeClient, req: SocketModeRequest) -> None:
        # Always ack immediately so Slack doesn't retry
        try:
            await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        except Exception:
            logger.debug("Failed to ack event (WebSocket not ready), skipping")
            return

        if req.type == "interactive":
            t = asyncio.create_task(dispatch_interactive(req.payload or {}))
            orch._handler_tasks.add(t)
            t.add_done_callback(orch._handler_tasks.discard)
            return

        if req.type == "slash_commands":
            payload = req.payload or {}
            t = asyncio.create_task(_handle_slash(orch, payload))
            orch._handler_tasks.add(t)
            t.add_done_callback(orch._handler_tasks.discard)
            return

        if req.type != "events_api":
            return

        event = (req.payload or {}).get("event", {})
        event_type = event.get("type")

        # ── Tracking-channel join → allowlist prompt ──
        if event_type == "member_joined_channel":
            _maybe_prompt_owner(orch, event)
            return

        # ── Home Tab ──
        if event_type == "app_home_opened":
            user = event.get("user")
            if event.get("tab") == "home" and user:
                if is_allowed_user(user):
                    sel().log_api_access(
                        caller=user,
                        operation="slack.home_tab",
                        outcome="allowed",
                        source="slack",
                    )
                    task = asyncio.ensure_future(_publish_home_tab(orch, user))
                    _background_tasks.add(task)
                    task.add_done_callback(_background_tasks.discard)
                else:
                    sel().log_api_access(
                        caller=user,
                        operation="slack.home_tab",
                        outcome="denied",
                        source="slack",
                        error="unauthorized sender",
                    )
            return

        # ── Messages and mentions ──
        if event_type not in ("message", "app_mention"):
            return
        _bot_id = event.get("bot_id")
        _subtype = event.get("subtype")
        # ── message_deleted: cancel queued or in-flight messages ──
        if _subtype == "message_deleted":
            await _handle_message_deleted(orch, event)
            return
        if _subtype and _subtype != "file_share":
            return
        if _bot_id:  # pragma: no cover — socket mode callback, tested via integration
            sel().log_api_access(
                caller=_bot_id,
                operation="slack.message",
                outcome="denied",
                source="slack",
                error="untrusted_bot",
            )
            return

        # Enterprise Grid: envelope team_id is the *bot's* workspace;
        # event["team"] may be the *sender's* workspace in shared channels.
        # Always prefer envelope to prevent cross-workspace bypass.
        outer_team = (req.payload or {}).get("team_id", "")
        if outer_team:
            event["team"] = outer_team
        elif not event.get("team"):
            logger.warning(
                "Enterprise Grid: no team_id from event or envelope " "(sender=%s) — rejecting",
                event.get("user", "unknown"),
            )
            sel().log_api_access(
                caller=event.get("user", "unknown"),
                operation="slack.message",
                outcome="denied",
                source="slack",
                error="missing_team_id",
            )
            return

        await _route_message(
            orch,
            event,
            seen,
            is_mention=(event_type == "app_mention"),
            from_trusted_bot=False,
        )

    orch._socket_client.socket_mode_request_listeners.append(_on_event)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Home Tab
# ---------------------------------------------------------------------------


async def _publish_home_tab(orch: GatewayOrchestrator, user_id: str) -> None:
    """Build and publish the Block Kit Home Tab view."""
    try:
        blocks: list[dict] = []

        # ── Data Handling Reminder ──
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        ":warning: *Do not enter sensitive or confidential data"
                        " into Kiro Crew.* Follow your organization's data handling"
                        " policy when using this tool."
                    ),
                },
            }
        )
        blocks.append({"type": "divider"})

        # ── Status ──
        yolo = is_yolo_mode()
        blocks.append(
            {"type": "header", "text": {"type": "plain_text", "text": "👻 Kiro Crew Status"}}
        )
        status_lines = [
            "*Gateway:* ✅ Online",
            f"*YOLO mode:* {'🟢 ON' if yolo else '🔴 OFF'}",
        ]
        if orch.sessions is not None:
            status_lines.append(f"*Active sessions:* {orch.sessions.count}")
        status_lines.append(f"*Uptime:* {Stats().uptime_str()}")
        status_lines.append(await current_context().identity.status_line())
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(status_lines)}}
        )
        blocks.append({"type": "divider"})

        # ── Capabilities ──
        blocks.append({"type": "header", "text": {"type": "plain_text", "text": "🔌 Capabilities"}})
        try:
            servers = list_servers()
            skills = _get_skills_loader().list_skills()

            # Slack caps a single section's text at 3000 chars. MCP servers and
            # skills each get their OWN section with an independent length cap
            # (appending "…and N more" when the list won't fit) — an uncapped
            # list (e.g. 100+ skills) would overflow and make views.publish fail
            # with invalid_arguments, breaking the whole Home tab. Mirrors the
            # cron block's jobs[:15] guard below.
            def _capped_names_section(
                label: str, names: list[str], budget: int = 2900
            ) -> dict:
                total = len(names)
                prefix = f"*{label} ({total}):* "
                suffix_room = 24  # reserve for "  _…and N more_"
                shown: list[str] = []
                used = len(prefix)
                for nm in names:
                    add = (", " if shown else "") + nm
                    if used + len(add) > budget - suffix_room:
                        break
                    shown.append(nm)
                    used += len(add)
                line = prefix + ", ".join(shown)
                if len(shown) < total:
                    line += f"  _…and {total - len(shown)} more_"
                # Defense-in-depth redaction ("never trust output").
                line = redact_credentials(redact_exfiltration_urls(line)[0])[0]
                return {"type": "section", "text": {"type": "mrkdwn", "text": line}}

            if servers:
                blocks.append(
                    _capped_names_section("MCP Integrations", [s.name for s in servers])
                )
            if skills:
                blocks.append(
                    _capped_names_section("Skills", [s["name"] for s in skills])
                )
            if not servers and not skills:
                blocks.append(
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "_No MCP servers or skills configured._",
                        },
                    }
                )
        except Exception:
            logger.error("Failed to load capabilities for home tab", exc_info=True)
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "_Capabilities unavailable._"},
                }
            )
        blocks.append({"type": "divider"})

        # ── Cron Jobs ──
        blocks.append({"type": "header", "text": {"type": "plain_text", "text": "⏰ Cron Jobs"}})
        if orch.cron_svc is not None:
            jobs = orch.cron_svc.list_jobs(include_disabled=True)
            if jobs:
                try:
                    _tz = KiroCrewConfig.load().timezone
                except Exception:
                    _tz = ""
                if not _tz and orch.slack is not None:
                    try:
                        profile = await orch.slack.get_user_profile(user_id)
                        _tz = profile.get("timezone", "")
                    except Exception:
                        _tz = ""
                lines = []
                for j in jobs[:15]:
                    status = "✅" if j.enabled else "⏸️"
                    sched = format_schedule(j.schedule, tz_name=_tz)
                    raw = f"{status} *{j.name}* — `{sched}`"
                    lines.append(redact_credentials(redact_exfiltration_urls(raw)[0])[0])
                if len(jobs) > 15:
                    lines.append(f"_…and {len(jobs) - 15} more_")
                blocks.append(
                    {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}
                )
            else:
                blocks.append(
                    {"type": "section", "text": {"type": "mrkdwn", "text": "_No cron jobs._"}}
                )
        else:
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "_Cron service unavailable._"},
                }
            )
        blocks.append({"type": "divider"})

        # ── Sessions (main chat + autopilot/task runner) ──
        blocks.append({"type": "header", "text": {"type": "plain_text", "text": "🧵 Sessions"}})
        # Deny-by-default authorization gate (defense-in-depth).
        #
        # Session JSONLs contain prior conversation contents. The dispatcher
        # already filters ``app_home_opened`` events via ``is_allowed_user``
        # at events.py before calling _publish_home_tab, so in production
        # this branch is unreachable today. The check is still required by
        # the security-controls rule (deny-by-default) and protects
        # against future refactors that bypass the dispatcher gate. Mirrors
        # the slash-command pattern at events._handle_sessions.
        if not (is_owner(user_id) or is_allowed_user(user_id)):
            sel().log_api_access(
                caller=user_id,
                operation="slack.home_tab_sessions_data_access",
                outcome="denied",
                source="slack",
                resources="home_tab",
                error="unauthorized caller",
            )
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "_Sessions unavailable._"},
                }
            )
            blocks.append({"type": "divider"})
            # Skip ahead to the next section (lessons), bypassing the
            # try block below which would otherwise read sessions.
        else:
            try:
                sess_mgr = orch.sessions
                # Read per-kind cap from config (default 5).
                try:
                    per_kind = orch._cfg.slack.home_tab_sessions_per_kind
                    if not isinstance(per_kind, int) or per_kind < 1:
                        per_kind = _HOME_TAB_SESSIONS_PER_KIND
                except (AttributeError, TypeError):
                    per_kind = _HOME_TAB_SESSIONS_PER_KIND
                # Single directory scan for both kinds; partition + cap in memory.
                all_rows = _collect_recent_sessions(
                    sess_mgr,
                    limit=per_kind * 10,
                    kind=(_SESSION_KIND_DASHBOARD, _SESSION_KIND_TASKRUNNER),
                )
                sel().log_api_access(
                    caller=user_id,
                    operation="slack.home_tab_sessions_data_access",
                    outcome="allowed",
                    source="slack",
                    resources=f"{len(all_rows)} sessions read",
                )
                dashboard_rows = [r for r in all_rows if r["kind"] == _SESSION_KIND_DASHBOARD][
                    :per_kind
                ]
                taskrunner_rows = [r for r in all_rows if r["kind"] == _SESSION_KIND_TASKRUNNER][
                    :per_kind
                ]
                if dashboard_rows or taskrunner_rows:
                    if dashboard_rows:
                        blocks.append(
                            {
                                "type": "context",
                                "elements": [{"type": "mrkdwn", "text": "*Main chat*"}],
                            }
                        )
                        blocks.extend(_build_sessions_blocks(dashboard_rows, for_home_tab=True))
                    if taskrunner_rows:
                        if dashboard_rows:
                            blocks.append({"type": "divider"})
                        blocks.append(
                            {
                                "type": "context",
                                "elements": [
                                    {"type": "mrkdwn", "text": "*Autopilot / task runner*"}
                                ],
                            }
                        )
                        blocks.extend(_build_sessions_blocks(taskrunner_rows, for_home_tab=True))
                else:
                    blocks.append(
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": "_No recent sessions._"},
                        }
                    )
            except Exception as exc:
                # Redact-then-truncate the exception message before writing
                # it to SEL ``error=``, mirroring the slash and keyword
                # error-path patterns. SEL forwards externally redact, but
                # the on-disk audit file is not internally redacted, so this
                # is defense-in-depth for the security-controls
                # "never trust output" rule applied to exception messages.
                redacted_exc, _ = redact_exfiltration_urls(str(exc))
                redacted_exc, _ = redact_credentials(redacted_exc)
                logger.exception("home_tab sessions: collector failed for user %s", user_id)
                # SEL audit must record the access attempt even when the collector
                # raises, so a failure mode can't silently bypass the audit trail.
                # The success-path audit at the top of the try is skipped on
                # exception; this is the only audit that fires in that case.
                try:
                    sel().log_api_access(
                        caller=user_id,
                        operation="slack.home_tab_sessions_data_access",
                        outcome="error",
                        source="slack",
                        resources="0 sessions read (collector failed)",
                        error=redacted_exc[:200],
                    )
                except Exception:
                    logger.exception("Failed to emit SEL audit for home tab sessions error")
                blocks.append(
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": "_Sessions unavailable._"},
                    }
                )
            blocks.append({"type": "divider"})

        # ── Recent Lessons ──
        blocks.append(
            {"type": "header", "text": {"type": "plain_text", "text": "📚 Recent Lessons"}}
        )
        lesson_lines: list[str] = []
        total_lessons = 0
        vs_ok = False
        # Primary: read from vector store (where learn_add writes).
        vs = getattr(orch, "vector_memory", None)
        if vs is not None and callable(getattr(vs, "get_lessons", None)):
            try:
                all_vs = await asyncio.to_thread(vs.get_lessons)
            except Exception:
                all_vs = None
                logger.debug("Vector store lesson read failed, trying JSONL", exc_info=True)
            if isinstance(all_vs, list):
                total_lessons = len(all_vs)
                # get_lessons() returns ORDER BY updated_at DESC (most recent first).
                for entry in all_vs[:5]:
                    try:
                        parsed = json.loads(entry["value_json"])
                        rule = (
                            parsed.get("rule", str(parsed))
                            if isinstance(parsed, dict)
                            else str(parsed)
                        )
                        lesson_lines.append(
                            f"• {redact_credentials(redact_exfiltration_urls(rule)[0])[0][:100]}"
                        )
                    except Exception:
                        logger.debug("Skipping malformed lesson entry", exc_info=True)
                vs_ok = True
        # Fallback: legacy JSONL store.
        if not vs_ok and orch.ctx_builder is not None:
            all_lessons = orch.ctx_builder.lessons.load_all()
            total_lessons = len(all_lessons)
            for le in all_lessons[-5:]:
                lesson_lines.append(
                    f"• {redact_credentials(redact_exfiltration_urls(le.rule)[0])[0][:100]}"
                )
        if lesson_lines:
            if total_lessons > 5:
                lesson_lines.append(f"_…and {total_lessons - 5} more_")
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lesson_lines)}}
            )
        elif not vs_ok and orch.ctx_builder is None:
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": "_Lessons unavailable._"}}
            )
        else:
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": "_No lessons yet._"}}
            )
        blocks.append({"type": "divider"})

        # ── Commands ──
        blocks.append({"type": "header", "text": {"type": "plain_text", "text": "⌨️ Commands"}})
        _sc = f"/{orch.slack_command}"
        for name, (_, desc) in sorted(SLASH_REGISTRY.items()):
            blocks.append(command_hint_block(f"{_sc} {name}", desc))
        blocks.append(command_hint_block(f"{_sc} #channel", "track/untrack channel"))

        # ── Version ──
        version_text = f"📦 Kiro Crew v{__version__}"
        update_info = get_update_info()
        remote_ver = update_info.get("remote_version")
        if update_info.get("available") and remote_ver is not None:
            version_text += f"  •  🆕 v{remote_ver} available — open Dashboard to update"
        version_text = redact_credentials(redact_exfiltration_urls(version_text)[0])[0]
        blocks.append({"type": "divider"})
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": version_text}]})

        view = {"type": "home", "blocks": blocks}

        if orch.slack is not None:
            await orch.slack.views_publish(user_id=user_id, view=view)
        else:
            logger.warning("Cannot publish home tab — Slack client is None")

    except Exception:
        logger.error("Failed to publish home tab for %s", user_id, exc_info=True)
        # Attempt fallback error view
        try:
            if orch.slack is not None:
                fallback = {
                    "type": "home",
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": "⚠️ Failed to load Home Tab. Try again later.",
                            },
                        }
                    ],
                }
                await orch.slack.views_publish(user_id=user_id, view=fallback)
        except Exception:
            logger.debug("Fallback home tab also failed", exc_info=True)


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


async def _handle_slash(orch: GatewayOrchestrator, payload: dict) -> None:
    """Route ``/kirocrew <sub-command>`` via :data:`SLASH_REGISTRY`.

    Falls back to @user / #channel mention handling, then help text.
    """
    cmd = payload.get("command", "")
    cmd_text = payload.get("text", "").strip()
    caller_id = payload.get("user_id", "")
    response_url = payload.get("response_url", "")
    logger.info("Slash command: %s %s (caller=%s)", cmd, _safe_log(cmd_text), caller_id)

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
        _spawn_tracked(_respond("⛔ You are not authorized to use this command."))
        return

    sel().log_api_access(
        caller=caller_id,
        operation="slack.slash_command",
        outcome="allowed",
        source="slack",
        resources=cmd_text,
    )

    if not (orch.slack and orch._owner_id):
        _spawn_tracked(_respond("⚠️ Owner not configured."))
        return

    # Parse sub-command and args
    parts = cmd_text.split(maxsplit=1)
    sub_cmd = parts[0].lower() if parts else ""
    args = parts[1] if len(parts) > 1 else ""

    # Registry lookup
    entry = SLASH_REGISTRY.get(sub_cmd)
    if entry is not None:
        handler, _ = entry
        # Stash trigger_id so modal-opening handlers can use it
        orch._last_trigger_id = payload.get("trigger_id", "")  # type: ignore[attr-defined]
        _spawn_tracked(handler(orch, caller_id, args, _respond))
        return

    # Fallback: @user mention — multi-user access disabled for security
    user_match = re.search(r"<@([A-Z0-9]+)(?:\|([^>]+))?>", cmd_text)
    if user_match:
        _spawn_tracked(
            _respond("⛔ Multi-user access is disabled. Only the owner can use Kiro Crew via Slack.")
        )
        return

    # Fallback: #channel mention — Slack sends <#C1234|name> or <#C1234>
    channel_match = re.search(r"<#([A-Z0-9]+)(?:\|([^>]*))?>", cmd_text)
    if channel_match:
        channel_id = channel_match.group(1)
        channel_name = channel_match.group(2) or "Secret"
        _spawn_tracked(
            prompt_track_channel(orch.slack, orch._owner_id, channel_id, channel_name)
        )
        _spawn_tracked(_respond(f"📨 Track request sent for #{channel_name or channel_id}."))
        return

    # Unknown sub-command → help
    _spawn_tracked(_respond(_build_help_text(orch.slack_command)))


# ---------------------------------------------------------------------------
# Tracking-channel join
# ---------------------------------------------------------------------------


def _maybe_prompt_owner(orch: GatewayOrchestrator, event: dict) -> None:
    """Multi-user access disabled — channel-join allowlist prompts are blocked."""
    return


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Audio transcription helper
# ---------------------------------------------------------------------------

# Single source of truth lives with the attachment adapter so the
# transcriber and the ingestion path cannot disagree about what is audio.
_AUDIO_MIMETYPES = SLACK_AUDIO_MIMETYPES


async def _transcribe_with_reaction(
    slack_client: "SlackClientOps",
    channel: str,
    msg_ts: str,
    orch: "GatewayOrchestrator",
    files: list[dict],
) -> list[str]:
    """Transcribe audio files with a reaction indicator for user feedback."""
    _stt_reaction_added = False
    try:
        await slack_client.add_reaction(channel, msg_ts, "studio_microphone")
        _stt_reaction_added = True
    except Exception:
        logger.debug("Failed to add STT reaction", exc_info=True)

    try:
        transcripts = await _transcribe_files(orch, files)
    finally:
        if _stt_reaction_added:
            try:
                await slack_client.remove_reaction(
                    channel,
                    msg_ts,
                    "studio_microphone",
                )
            except Exception:
                logger.debug("Failed to remove STT reaction", exc_info=True)
    return transcripts


async def _transcribe_files(orch: "GatewayOrchestrator", files: list[dict]) -> list[str]:
    """Download and transcribe audio files, return list of transcription strings."""
    results: list[str] = []
    for f in files:
        mimetype = f.get("mimetype", "")
        if not any(mimetype.startswith(prefix) for prefix in _AUDIO_MIMETYPES):
            continue
        url = f.get("url_private_download") or f.get("url_private", "")
        if not url:
            continue
        dest: str | None = None
        try:
            raw_ft = re.sub(r"[^a-zA-Z0-9]", "", f.get("filetype", "webm"))
            suffix = "." + (raw_ft or "webm")
            fd, dest = tempfile.mkstemp(suffix=suffix)
            os.close(fd)
            assert orch.slack is not None
            assert dest is not None
            await orch.slack.download_file(url, dest)
            sel().log_api_access(
                caller="stt",
                operation="slack.download_file",
                outcome="success",
                source="transcribe",
                resources=f.get("name", "?"),
            )
            transcript = await transcribe_audio(dest)
            sel().log_api_access(
                caller="stt",
                operation="whisper.transcribe",
                outcome="success" if transcript else "empty",
                source="transcribe",
                resources=f.get("name", "?"),
            )
            if transcript:
                results.append(transcript)
                logger.info("Transcribed voice memo: %d chars", len(transcript))
            else:
                logger.warning("Transcription returned empty for %s", f.get("name", "?"))
        except Exception:
            logger.exception("Failed to transcribe file %s", f.get("name", "?"))
            sel().log_api_access(
                caller="stt",
                operation="whisper.transcribe",
                outcome="error",
                source="transcribe",
                resources=f.get("name", "?"),
                error="transcription_failed",
            )
        finally:
            if dest:
                try:
                    os.unlink(dest)
                except OSError:
                    pass
    return results


# ---------------------------------------------------------------------------
# Message routing
# ---------------------------------------------------------------------------


async def _handle_message_deleted(orch: GatewayOrchestrator, event: dict) -> None:
    """Handle message_deleted subtype — cancel queued or in-flight messages."""
    deleted_ts = event.get("deleted_ts")
    _del_thread_ts = event.get("previous_message", {}).get("thread_ts")
    _del_channel = event.get("channel", "")
    _del_user = event.get("previous_message", {}).get("user", "")
    if deleted_ts and _del_channel and is_allowed_user(_del_user):
        _del_session_key = _del_thread_ts or deleted_ts
        was_queued = False
        if orch.sessions:
            was_queued = orch.sessions.cancel_queued(_del_session_key, deleted_ts)
        if not was_queued:
            _pq = orch._pending_queue.get(_del_session_key, [])
            _filtered = [item for item in _pq if item[0] != deleted_ts]
            if len(_filtered) < len(_pq):
                was_queued = True
                if _filtered:
                    orch._pending_queue[_del_session_key] = _filtered
                else:
                    orch._pending_queue.pop(_del_session_key, None)
        if was_queued:
            logger.info(
                "message_deleted: ts=%s session=%s queued=%s",
                deleted_ts,
                _del_session_key,
                was_queued,
            )
        sel().log_api_access(
            caller=event.get("previous_message", {}).get("user", "unknown"),
            operation="slack.message_deleted",
            outcome="allowed",
            source="slack",
            resources=f"ts={deleted_ts} session={_del_session_key} queued={was_queued}",
        )


def _resolve_approval_mode(orch: "GatewayOrchestrator") -> str:
    """Slack dispatch approval mode: CLI --approval flag wins, else config.

    Normalized to handle_message's auto/interactive contract; reads/yolo are
    gated separately (gateway approval-event path, global YOLO/trust).
    """
    # Runtime YOLO (owner-toggled via the `yolo` slash command, TTL-capped safety_override)
    # auto-approves all tools. The native loop checks is_yolo_mode() inline; the
    # transport TurnDriver only sees this resolved mode, so fold YOLO in here at
    # the single per-message chokepoint (evaluated fresh each message) — both
    # paths then honor the runtime toggle consistently.
    if is_yolo_mode():
        return APPROVAL_AUTO
    mode = orch._approval_mode or orch._cfg.agent.approval_mode
    return APPROVAL_AUTO if mode == APPROVAL_AUTO else APPROVAL_INTERACTIVE


async def _dispatch_queued(
    orch: GatewayOrchestrator,
    session_key: str,
    msg_ts: str,
    text: str,
    kwargs: dict,
) -> None:
    """Dispatch a queued message — remove ⏳ reaction and call handle_message."""
    channel = kwargs.get("channel", "")
    thread_ts = kwargs.get("thread_ts")
    if orch.slack:
        try:
            await orch.slack.remove_reaction(channel, msg_ts, "hourglass_flowing_sand")
        except Exception:
            pass
    # Route the queued follow-up through the SAME gate as the initial message so
    # behavior is consistent mid-conversation: a thread that took the transport
    # path must keep taking it for its queued follow-ups (not silently fall back
    # to native). Review-mode channels stay on native (privacy gate), matching
    # the _route_message gate.
    _activation = orch._cfg.channel_config(channel).activation
    _use_transport = (
        getattr(getattr(orch._cfg, "messaging", None), "use_transport", False) is True
        and _activation != ACTIVATION_REVIEW
    )
    try:
        if _use_transport:
            await handle_message_transport(
                orch.slack,  # type: ignore[arg-type]
                orch.sessions,  # type: ignore[arg-type]
                channel,
                text,
                thread_ts,
                msg_ts,
                kwargs.get("sender_id", ""),
                context_builder=orch.ctx_builder,
                conversation_log=orch.conv_log,
                approval_mode=_resolve_approval_mode(orch),
                agent_override=kwargs.get("agent_override"),
                subagent_manager=orch.subagent_mgr,
                task_runner=orch.task_runner,
                cron_service=orch.cron_svc,
                # Live-read per message (parity with native handle_message, which
                # loads config at handler.py:2661/2683): orch._cfg is captured at
                # startup, so reading it here would make settings-UI toggle saves
                # silently inert until restart.
                reactions_enabled=KiroCrewConfig.load().slack.reactions_enabled,
                show_thinking=KiroCrewConfig.load().slack.show_thinking,
                consolidator=orch.consolidator,
                user_display_name=kwargs.get("user_display_name"),
            )
            return
        await handle_message(
            orch.slack,  # type: ignore[arg-type]
            orch.sessions,  # type: ignore[arg-type]
            channel,
            text,
            thread_ts,
            msg_ts,
            kwargs.get("sender_id", ""),
            team_id=kwargs.get("team_id", ""),
            approval_mode=_resolve_approval_mode(orch),
            context_builder=orch.ctx_builder,
            cron_service=orch.cron_svc,
            conversation_log=orch.conv_log,
            consolidator=orch.consolidator,
            subagent_manager=orch.subagent_mgr,
            task_runner=orch.task_runner,
            channel_agent=kwargs.get("agent_override"),
            user_display_name=kwargs.get("user_display_name"),
        )
    finally:
        # The enqueue path deferred temp-image cleanup to here so the queued
        # turn's text could still resolve its image paths (see _route_message).
        # Unlink them now that the turn has consumed them — in finally so a
        # raising turn can't leak the temp files.
        for _p in kwargs.get("image_temp_paths") or []:
            try:
                os.unlink(_p)
            except OSError:
                pass


# Maximum characters to recover from block extraction (DoS guard).
# Slack message bodies can be ~40k; 16k is a safe recovery cap for downstream processing.
_MAX_RECOVERED_TEXT_CHARS = 16000


def _render_rich_text_element(el: dict) -> str:
    """Render a single rich_text inline element to plain text.

    Handles all documented Slack rich_text element types:
    text, link, emoji, user, usergroup, channel, broadcast, date.
    """
    if not isinstance(el, dict):
        return ""
    el_type = el.get("type")
    if el_type == "text":
        return el.get("text", "")
    if el_type == "link":
        # link: show "text (url)" if both present; else whichever exists
        text = el.get("text", "")
        url = el.get("url", "")
        if text and url:
            return f"{text} ({url})"
        return text or url
    if el_type == "emoji":
        # emoji: use :name: format; fall back to unicode if name missing
        name = el.get("name")
        if name:
            return f":{name}:"
        return el.get("unicode", "")
    if el_type == "user":
        user_id = el.get("user_id", "")
        return f"<@{user_id}>" if user_id else ""
    if el_type == "usergroup":
        usergroup_id = el.get("usergroup_id", "")
        return f"<!subteam^{usergroup_id}>" if usergroup_id else ""
    if el_type == "channel":
        channel_id = el.get("channel_id", "")
        return f"<#{channel_id}>" if channel_id else ""
    if el_type == "broadcast":
        # broadcast range: here, channel, or everyone
        range_val = el.get("range", "")
        return f"<!{range_val}>" if range_val else ""
    if el_type == "date":
        # date: use fallback text if present (human-readable rendering)
        return el.get("fallback", "")
    # Unknown element type — attempt text field, log for observability
    logger.debug("Unknown rich_text element type=%r, attempting text field", el_type)
    return el.get("text", "")


def _extract_blocks_text(blocks: list[dict]) -> str:
    """Extract readable text from Block Kit blocks (rich_text, section, context).

    Handles the common block types Slack uses for user messages and shared
    content.  Returns empty string if no text can be recovered.
    Defensive: never raises on malformed input.
    """
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "rich_text":
            elements = block.get("elements", [])
            if not isinstance(elements, list):
                elements = []
            for element in elements:
                if not isinstance(element, dict):
                    continue
                el_type = element.get("type")
                child_els = element.get("elements", [])
                if not isinstance(child_els, list):
                    child_els = []
                if el_type == "rich_text_list":
                    # Prefix each list item with "- " to preserve list structure.
                    # (Numbered vs bulleted distinction not preserved — simple bullet.)
                    for child in child_els:
                        if not isinstance(child, dict):
                            continue
                        sub_els = child.get("elements", [])
                        if not isinstance(sub_els, list):
                            sub_els = []
                        inline = "".join(
                            _render_rich_text_element(el) for el in sub_els
                        )
                        if inline:
                            parts.append(f"- {inline}")
                elif el_type == "rich_text_quote":
                    # Quote blocks: prefix with "> "
                    inline = "".join(
                        _render_rich_text_element(el) for el in child_els
                    )
                    if inline:
                        parts.append(f"> {inline}")
                else:
                    # rich_text_section, rich_text_preformatted
                    inline = "".join(
                        _render_rich_text_element(el) for el in child_els
                    )
                    if inline:
                        parts.append(inline)
        elif block_type == "section":
            text_obj = block.get("text")
            if isinstance(text_obj, dict):
                section_text = text_obj.get("text", "")
                if section_text:
                    parts.append(section_text)
        elif block_type == "context":
            ctx_elements = block.get("elements", [])
            if not isinstance(ctx_elements, list):
                ctx_elements = []
            for el in ctx_elements:
                if not isinstance(el, dict):
                    continue
                ctx_text = el.get("text", "")
                if ctx_text:
                    parts.append(ctx_text)
    result = "\n".join(parts).strip()
    if not result:
        return ""
    return result[:_MAX_RECOVERED_TEXT_CHARS]


# Slack's generic fallback strings for messages whose content lives in blocks.
# NOTE: These are best-effort, undocumented, English-only Slack placeholder strings.
# They may change or be localized — recovery is best-effort for non-English workspaces.
# No fuzzy/structural detection is attempted (out of scope; would change behavior broadly).
_SLACK_BLOCK_FALLBACKS = frozenset({
    "This message contains interactive elements.",
    "This content can't be displayed.",
})


def _normalize_message_blocks(raw: list) -> list[dict]:
    """Drill into the Slack message_blocks wrapper structure.

    ``message_blocks`` is a wrapper list:
    ``[{"team":..., "channel":..., "ts":..., "message": {"blocks": [...]}}]``
    This extracts and flattens the inner blocks from each wrapper.
    """
    result: list[dict] = []
    if not isinstance(raw, list):
        return result
    for item in raw:
        if not isinstance(item, dict):
            continue
        msg = item.get("message")
        if isinstance(msg, dict):
            inner_blocks = msg.get("blocks")
            if isinstance(inner_blocks, list):
                result.extend(b for b in inner_blocks if isinstance(b, dict))
    return result


def _extract_shared_text(event: dict) -> str:
    """Recover message text from forwarded-message attachments.

    Slack forwards carry their content in the ``attachments`` array (entries
    flagged ``is_share`` / ``is_msg_unfurl``), not in the top-level ``text``
    field. Link-unfurl attachments are excluded so pasted URLs don't leak
    preview text into the routed message body.

    When the attachment's ``text`` is empty and ``fallback`` is a generic
    Slack placeholder (e.g. "This message contains interactive elements."),
    attempts to reconstruct content from the attachment's ``blocks`` or the
    event-level ``blocks`` array.
    """
    attachments = event.get("attachments") or []
    parts: list[str] = []
    for att in attachments:
        if not (att.get("is_share") or att.get("is_msg_unfurl")):
            continue
        att_text = att.get("text") or ""
        if att_text:
            parts.append(att_text)
            continue
        # text is empty — try blocks before falling back to the generic fallback.
        # att["blocks"] is already a flat block list; att["message_blocks"] is a
        # wrapper list that must be normalized first.
        att_blocks = att.get("blocks")
        if isinstance(att_blocks, list) and att_blocks:
            extracted = _extract_blocks_text(att_blocks)
            if extracted:
                parts.append(extracted)
                continue
        msg_blocks = att.get("message_blocks")
        if msg_blocks:
            normalized = _normalize_message_blocks(msg_blocks)
            if normalized:
                extracted = _extract_blocks_text(normalized)
                if extracted:
                    parts.append(extracted)
                    continue
        # Last resort: use fallback unless it's a generic Slack placeholder
        fallback = att.get("fallback") or ""
        if fallback and fallback not in _SLACK_BLOCK_FALLBACKS:
            parts.append(fallback)
    # If attachments yielded nothing, try event-level blocks (Slack sometimes
    # puts the real content there for shared messages).
    if not parts:
        event_blocks = event.get("blocks") or []
        if event_blocks:
            extracted = _extract_blocks_text(event_blocks)
            if extracted:
                return extracted
    return "\n\n".join(part for part in parts if part).strip()


async def _route_message(
    orch: GatewayOrchestrator,
    event: dict,
    seen: SeenCache,
    is_mention: bool = False,
    from_trusted_bot: bool = False,
) -> None:
    """Validate, dedup, check activation mode, and dispatch an incoming Slack message."""
    sender_id = event.get("user", "") or (event.get("bot_id", "") if from_trusted_bot else "")
    channel = event.get("channel", "")
    text = event.get("text", "")
    thread_ts = event.get("thread_ts")
    msg_ts = event.get("ts", "")
    team_id = event.get("team", "")
    files = event.get("files", [])

    # Slack forwards carry content in attachments, not text — recover it so the
    # forward isn't silently dropped by the (not text and not files) guard below.
    # Also recover when Slack sets text to a generic Block Kit fallback placeholder.
    if not text or text in _SLACK_BLOCK_FALLBACKS:
        fallback = "" if text in _SLACK_BLOCK_FALLBACKS else text
        text = _extract_shared_text(event) or fallback

    logger.debug("Stream debug: team_id=%s user_id=%s channel=%s", team_id, sender_id, channel)

    if not sender_id or not channel or (not text and not files):
        return

    # ── Enterprise origin check: reject messages from swapped tokens ──
    # Per-message gate via the active PlatformContext (default-open; Amazon
    # companion fail-closed).
    if not current_context().slack_gate.check_message_origin(team_id):
        logger.error("Message rejected: team_id=%s does not match validated workspace", team_id)
        sel().log_api_access(
            caller=sender_id,
            operation="slack.message",
            outcome="denied",
            source="slack",
            resources=f"team_id={team_id} channel={channel}",
            error="enterprise_origin_mismatch",
        )
        return

    # ── Workspace routing cache for org-wide installs ──
    # Slack Web API calls (chat.postMessage, chat.startStream, etc.) need
    # team_id when the bot is org-wide installed; record the channel→team
    # mapping so outbound posts on this channel route to the correct
    # workspace and avoid ``team_access_not_granted``.
    record_team = getattr(orch.slack, "record_channel_team", None)
    if record_team and team_id:
        record_team(channel, team_id)

    # ── Access control: record authorization decision early for SEL audit ──
    # The ephemeral rejection is deferred until after activation checks so
    # users in observe/mention channels aren't spammed, but the SEL event
    # is always emitted to preserve the audit trail.
    _user_authorized = is_allowed_user(sender_id)
    if _user_authorized:
        sel().log_api_access(
            caller=sender_id,
            operation="slack.message",
            outcome="allowed",
            source="slack",
        )
    else:
        logger.warning("Ignoring message from unauthorized user %s", sender_id)
        sel().log_api_access(
            caller=sender_id,
            operation="slack.message",
            outcome="denied",
            source="slack",
            error="unauthorized sender",
        )

    # ── Message-interceptor seam (Default: PROCESS = inline, OSS-identical) ──
    # An edition may intercept the message here and turn it into an out-of-band
    # challenge-redirect (e.g. a presigned dashboard-session link) instead of
    # processing it inline. The public DefaultSlackEnterpriseGate ALWAYS returns
    # PROCESS and cannot raise, so the standalone build falls straight through
    # exactly as before — the fallback below is never reached in standalone.
    #
    # ORDERING (security-critical): this runs BEFORE any content is recorded or
    # processed — the observe-mode channel_history.push below, audio transcription,
    # image/file download, and the non-observe history push all follow. An
    # unverified sender's message content (prompt-injection text, attachments) must
    # NOT be persisted to channel history before the gate decides: otherwise a
    # later VERIFIED turn in the same channel could pull that stored content into
    # agent context, bypassing the very challenge gate the edition relies on. So
    # the interceptor is the first thing after the user-allowlist check, before
    # the message leaves a trace. It keys on (sender_id, channel) identity, so it
    # does not need the post-transcription/mention-stripped text; it gets the raw
    # text for challenge context/logging only.
    #
    # The fallback is DROPPED (deny-by-default), NOT PROCESS: this branch is only
    # reached when a COMPOSED gate raised a transient error (a
    # PlatformCompositionError is re-raised by safe_context_call, never degraded).
    # A composed edition installs its interceptor precisely to keep unverified
    # traffic away from the agent, so an erroring interceptor must fail CLOSED —
    # degrading to PROCESS would let a message bypass the gate (deny-by-default).
    #
    # Gated on _user_authorized: an UNauthorized sender is not a challenge
    # candidate (they are rejected with an ephemeral below) and the observe-mode
    # history push is itself _user_authorized-gated, so only an authorized sender's
    # content can be recorded pre-gate — that is exactly the leak this ordering
    # closes. Skipping the interceptor for unauthorized senders also preserves
    # their existing ephemeral-rejection UX unchanged.
    if _user_authorized:
        _intercept_clean = text
        if is_mention and text.startswith("<@"):
            _end = text.find(">")
            if _end != -1:
                _intercept_clean = text[_end + 1 :].lstrip()
        # Interceptor-specific dedup: Slack re-delivers the same event on ack
        # timeout, and this seam runs BEFORE the main _route_message dedup (which
        # must stay after the activation check). Without a guard here a redirecting
        # adapter would re-mint + re-post a challenge link on every retry. Key it in
        # a separate "intercept:" namespace and RECORD it only on a non-PROCESS
        # decision (below) — never for PROCESS, so a passed message still reaches
        # the main dedup and the paired app_mention/message dual-event is preserved.
        _intercept_seen_key = f"intercept:{msg_ts}"
        if seen.check(_intercept_seen_key):
            # A prior delivery of this event already got a challenge/deny verdict;
            # drop the retry silently (the original already replied / was audited).
            return
        _decision = safe_context_call(
            lambda: current_context().slack_gate.intercept_message(
                orch,
                channel=channel,
                sender_id=sender_id,
                clean_text=_intercept_clean,
                thread_ts=thread_ts,
                msg_ts=msg_ts,
            ),
            fallback=InterceptDecision.DROPPED,
            log_message="slack_gate.intercept_message failed; failing closed (DROPPED)",
        )
        if _decision is not InterceptDecision.PROCESS:
            # REDIRECTED (a challenge was issued) or DROPPED (denied / gate error):
            # the gate owns any user-facing reply; short-circuit before ANY content
            # is recorded/processed. No image temps have been downloaded yet at this
            # point (that happens further below), so nothing to clean up here.
            seen.add(_intercept_seen_key)  # dedup subsequent retries of this event
            # SEL audit: the interceptor is a permission decision distinct from the
            # earlier allowlist check, so its verdict MUST reach the audit trail
            # (backend-security-controls). REDIRECTED = a challenge was issued;
            # DROPPED = denied or a fail-closed gate error.
            sel().log_api_access(
                caller=sender_id,
                operation="slack.message.intercept",
                outcome="denied",
                source="slack",
                resources=channel,
                error=f"intercept={getattr(_decision, 'value', _decision)}",
            )
            logger.info(
                "Message from %s in %s intercepted (%s)",
                sender_id,
                channel,
                getattr(_decision, "value", _decision),
            )
            return

    # ── Channel activation mode (checked BEFORE ephemeral & dedup) ──
    # When activation=mention, Slack sends both a `message` and an
    # `app_mention` event for the same msg_ts.  We must skip the plain
    # `message` event *without* marking it as seen so the subsequent
    # `app_mention` event is still processed.
    ch_cfg = orch._cfg.channel_config(channel)
    activation = ch_cfg.activation

    if activation == ACTIVATION_OFF:
        # Allow !channel commands through so the owner can re-enable the channel.
        # Text may start with "<@BOTID> " when @mentioned, so strip that first.
        _stripped = text.lstrip()
        if _stripped.startswith("<@"):
            end = _stripped.find(">")
            if end != -1:
                _stripped = _stripped[end + 1 :].lstrip()
        if not _stripped.startswith("!channel"):
            logger.debug("Channel %s activation=off — ignoring message", channel)
            sel().log_api_access(
                caller=sender_id,
                operation="slack.message",
                outcome="denied",
                source="slack",
                resources=channel,
                error="activation=off",
            )
            return

    # ── Inbound channels-governance gate (off-loop) — BEFORE any side effect ──
    # A ``channels`` policy that denies ``slack`` must stop inbound processing
    # before _route_message does anything observable: the observe-mode and
    # non-observe ``channel_history.push`` (denied content must not be recorded,
    # or a later ALLOWED turn in the channel could pull it into agent context),
    # sender display-name lookups, audio transcription, image/file downloads, the
    # ``!restart`` bang alias (a gateway restart), and session queueing/dispatch.
    # The gate inside handle_message() runs too late for this path — every one of
    # those side effects precedes it — so we gate HERE, right after auth /
    # interceptor / activation-off and before the first side effect.
    #
    # EXEMPT only cancellation (``!stop``): a denied channel must still be able to
    # halt a runaway session it previously started. ``!restart`` is NOT
    # cancellation and stays gated. Default OSS build (no ``channels`` policy)
    # permits, so this is byte-identical to today. handle_message keeps its own
    # gate as defense-in-depth for its other entry points (interaction
    # re-dispatch, synthetic sends).
    #
    # The exemption requires a PURE cancellation: text is exactly ``!stop`` AND
    # there are NO attachments. A ``!stop`` message carrying files/voice is not a
    # real cancellation — the transcription/attachment injection below rewrites
    # ``text`` so the downstream ``clean_text == "!stop"`` intercept no longer
    # matches — and exempting it would let the denied channel still DOWNLOAD the
    # attachment, TRANSCRIBE the voice memo, and PUSH the content to
    # channel_history (the very leak this gate prevents) before any turn is
    # blocked. So an attachment-bearing ``!stop`` is gated like any other message.
    _gate_text = text
    if is_mention and _gate_text.startswith("<@"):
        _gt_end = _gate_text.find(">")
        if _gt_end != -1:
            _gate_text = _gate_text[_gt_end + 1 :].lstrip()
    _is_pure_stop = _gate_text.strip().lower() == "!stop" and not files
    if not _is_pure_stop:
        if not await channel_inbound_permitted("slack"):
            logger.info("slack inbound dropped: denied by channels governance policy")
            sel().log_api_access(
                caller=sender_id,
                operation="slack.message",
                outcome="denied",
                source="slack",
                resources=channel,
                error="channels governance policy",
            )
            return

    # Resolve sender's Slack display name so the LLM uses the actual
    # profile name instead of guessing from memory. Cached on channel
    # history (for history context) and passed to handle_message.
    _sender_display: str | None = None
    if orch.channel_history:
        _sender_display = orch.channel_history._user_names.get(sender_id)
    if not _sender_display and orch.slack and hasattr(orch.slack, "get_user_info"):
        try:
            info = await orch.slack.get_user_info(sender_id)
            _sender_display = info.get("real_name") or sender_id
            if orch.channel_history:
                orch.channel_history.set_user_name(sender_id, _sender_display)
        except Exception:
            logger.debug("Failed to resolve display name for %s", sender_id, exc_info=True)

    # Fallback: if display name is still the raw Slack ID, resolve from
    # allowed_users config (works even without Slack users:read scope).
    if (not _sender_display or _sender_display == sender_id) and hasattr(orch, "_cfg"):
        for u in getattr(orch._cfg.slack, "allowed_users", []):
            if u.get("slack_id") == sender_id and u.get("name"):
                _sender_display = u["name"]
                if orch.channel_history:
                    orch.channel_history.set_user_name(sender_id, u["name"])
                break

    # Observe mode: record history from authorized users only, so non-owner
    # messages cannot influence LLM context.
    if activation == ACTIVATION_OBSERVE:
        if should_record_observe_history(orch.channel_history, _user_authorized):
            assert orch.channel_history is not None  # narrowed by helper
            orch.channel_history.push(channel, sender_id, text, thread_ts=thread_ts, msg_ts=msg_ts)
        if not is_mention:
            in_active_thread = (
                ch_cfg.thread_follow
                and thread_ts
                and orch.sessions
                and (
                    orch.sessions.has_session(thread_ts)
                    or orch.sessions.get_session_for_thread(thread_ts)
                    or (orch.conv_log and orch.conv_log.has_log(thread_ts))
                )
            )
            if not in_active_thread:
                sel().log_api_access(
                    caller=sender_id,
                    operation="slack.message",
                    outcome="denied",
                    source="slack",
                    resources=channel,
                    error="activation=observe, no mention or active thread",
                )
                return

    if activation in (ACTIVATION_MENTION, ACTIVATION_REVIEW) and not is_mention:
        # In mention/review mode: ignore messages without @mention UNLESS the
        # message is a reply in a thread where the bot already has an active
        # session (i.e., the bot was previously @mentioned in that thread).
        # When thread_follow=false, always require @mention even in active threads.
        in_active_thread = (
            ch_cfg.thread_follow
            and thread_ts
            and orch.sessions
            and (
                orch.sessions.has_session(thread_ts)
                or orch.sessions.get_session_for_thread(thread_ts)
                or (orch.conv_log and orch.conv_log.has_log(thread_ts))
            )
        )
        if not in_active_thread:
            sel().log_api_access(
                caller=sender_id,
                operation="slack.message",
                outcome="denied",
                source="slack",
                resources=channel,
                error=f"activation={activation}, no mention or active thread",
            )
            return

    # ── Access control: send ephemeral rejection ──
    # Only reached for messages the bot would actually respond to,
    # preventing notification spam in observe/mention channels.
    if not _user_authorized:
        if orch.slack:
            try:
                await orch.slack.post_ephemeral(
                    channel,
                    sender_id,
                    "⛔ You are not authorized to use this bot. "
                    "Ask the owner to add you to the allowlist.",
                )
            except Exception:
                logger.debug("Failed to send ephemeral rejection", exc_info=True)
        return

    # Dedup AFTER activation check — prevents the plain `message` event
    # from poisoning the cache before the `app_mention` event arrives.
    if seen.check_and_add(msg_ts):
        return

    # ── Transcribe audio files (voice memos) ──
    # Placed after dedup + auth to avoid expensive work on duplicate events
    # or unauthorized users.
    _image_temp_paths: list[str] = []
    _had_voice_input = False
    if files and orch.slack and _user_authorized:
        if stt_available():
            transcripts = await _transcribe_with_reaction(
                orch.slack,
                channel,
                msg_ts,
                orch,
                files,
            )
            if transcripts:
                raw = "\n".join(transcripts)
                raw, _ = redact_exfiltration_urls(raw)
                raw, _ = redact_credentials(raw)
                prefix = f"[Voice memo transcription]\n{raw}\n[End of transcription]"
                text = f"{prefix}\n\n{text}" if text else prefix
                _had_voice_input = True

        # ── Process non-audio files (images, text, etc.) ──
        image_paths, text_blocks = await process_slack_files(orch, files)
        _image_temp_paths = image_paths

        # Inject image paths so AcpClient._send_prompt() inlines them as base64
        if image_paths:
            paths_text = "\n".join(image_paths)
            text = f"{text}\n{paths_text}" if text else paths_text

        # Inject text file contents
        if text_blocks:
            blocks_text = "\n\n".join(text_blocks)
            text = f"{text}\n\n{blocks_text}" if text else blocks_text

    # Bail out if we still have no text after attempting transcription
    if not text:
        # Clean up any downloaded image temp files
        for p in _image_temp_paths:
            try:
                os.unlink(p)
            except OSError:
                pass
        return

    def _cleanup_image_temps() -> None:
        for p in _image_temp_paths:
            try:
                os.unlink(p)
            except OSError:
                pass

    # Record messages in channel history buffer (observe channels already
    # pushed above, so skip them here to avoid duplicates).
    if activation != ACTIVATION_OBSERVE:
        if orch.channel_history is None:
            logger.error("channel_history not initialised — skipping history push")
        else:
            orch.channel_history.push(channel, sender_id, text, thread_ts=thread_ts, msg_ts=msg_ts)

    # Strip the leading bot @mention so the LLM sees clean text.
    # app_mention events always start with "<@BOTID> ..." — just slice past the first ">".
    clean_text = text
    if is_mention and text.startswith("<@"):
        end = text.find(">")
        if end != -1:
            clean_text = text[end + 1 :].lstrip()
    if not clean_text:
        _cleanup_image_temps()
        return

    # ── !stop: intercept BEFORE handle_message to bypass session semaphore ──
    if clean_text.strip().lower() == "!stop":
        if not (is_owner(sender_id) or is_allowed_user(sender_id)):
            sel().log_api_access(
                caller=sender_id,
                operation="slack.stop_command",
                outcome="denied",
                source="slack",
                resources="!stop",
                error="unauthorized sender",
            )
            if orch.slack:
                await orch.slack.post_message(channel, "⛔ Not authorized.", thread_ts or msg_ts)
            return
        if not orch.sessions:
            sel().log_tool_invocation(
                session_key=thread_ts or msg_ts,
                source="slack",
                tool_name="!stop",
                tool_kind="command",
                outcome="no_session",
                metadata={"user": sender_id, "channel": channel},
            )
            if orch.slack:
                await orch.slack.post_message(channel, "Nothing running.", thread_ts or msg_ts)
            return
        session_key = thread_ts or msg_ts
        has_session = orch.sessions.has_session(session_key)
        active_task = orch._session_tasks.pop(session_key, None)
        if has_session or active_task:
            orch.sessions.clear_queue(session_key)
            orch._pending_queue.pop(session_key, None)

            # Post ephemeral "Stopping…" block with Kill Now button
            if orch.slack:
                await orch.slack.post_ephemeral(
                    channel,
                    sender_id,
                    "Stopping…",
                    blocks=build_stopping_blocks(session_key),
                    thread_ts=session_key,
                )

            async def _on_soft() -> None:
                if orch.slack:
                    await orch.slack.post_message(channel, "⏹ Execution stopped.", session_key)

            async def _on_hard() -> None:
                if orch.slack:
                    await orch.slack.post_message(
                        channel, "⛔ Execution stopped — session reset.", session_key
                    )

            outcome = await orch.sessions.stop_turn(session_key, on_soft=_on_soft, on_hard=_on_hard)
            if active_task and not active_task.done():
                active_task.cancel()
            # If stop_turn returned "idle" (no active turn), neither callback
            # fired — dismiss the stale "Stopping…" ephemeral explicitly.
            if outcome == "idle" and orch.slack:
                await orch.slack.post_message(channel, "Nothing running.", session_key)
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="!stop",
                tool_kind="command",
                outcome=outcome,
                metadata={"user": sender_id, "channel": channel},
            )
        else:
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="!stop",
                tool_kind="command",
                outcome="no_session",
                metadata={"user": sender_id, "channel": channel},
            )
            if orch.slack:
                await orch.slack.post_message(channel, "Nothing running.", thread_ts or msg_ts)
        return

    # ── !restart: bang alias for /kirocrew restart — intercept here so it
    #    never reaches the LLM session. Delegates to the slash handler
    #    (_handle_restart) which owns owner-check + supervisor guard, keeping
    #    a single source of truth for the restart logic. ──
    if clean_text.strip().lower() == "!restart":
        async def _restart_respond(text: str, **_kw: Any) -> None:
            if orch.slack:
                await orch.slack.post_message(channel, text, thread_ts or msg_ts)

        await _handle_restart(orch, sender_id, "", _restart_respond)
        return

    # Per-channel agent override
    agent_override = ch_cfg.agent or None

    logger.info(
        "Message from %s in %s (activation=%s): %s",
        sender_id,
        channel,
        activation,
        _safe_log(text[:80]),
    )

    # ── Queue check: if session is busy, enqueue instead of blocking ──
    session_key = thread_ts or msg_ts
    _task_busy = session_key in orch._session_tasks
    if _task_busy:
        # A task is already running for this session key.  Try the session-level
        # queue first (semaphore-based); fall back to an orchestrator-level
        # pre-session queue when the session object doesn't exist yet.
        _queued = orch.sessions and orch.sessions.enqueue(
            session_key,
            msg_ts,
            clean_text,
            force=True,
            channel=channel,
            thread_ts=thread_ts,
            sender_id=sender_id,
            team_id=team_id,
            agent_override=agent_override,
            user_display_name=_sender_display,
            image_temp_paths=list(_image_temp_paths),
        )
        if not _queued:
            # Session object not created yet — stash on orch._pending_queue
            orch._pending_queue.setdefault(session_key, []).append(
                (
                    msg_ts,
                    clean_text,
                    dict(
                        channel=channel,
                        thread_ts=thread_ts,
                        sender_id=sender_id,
                        team_id=team_id,
                        agent_override=agent_override,
                        user_display_name=_sender_display,
                        image_temp_paths=list(_image_temp_paths),
                    ),
                )
            )
        logger.info(
            "Message %s queued for busy session %s (session_obj=%s)", msg_ts, session_key, _queued
        )
        if orch.slack:
            try:
                await orch.slack.add_reaction(channel, msg_ts, "hourglass_flowing_sand")
            except Exception:
                logger.debug("Failed to add queue reaction", exc_info=True)
        # NOTE: do NOT _cleanup_image_temps() here — clean_text references these
        # temp-file paths and the queued turn hasn't run yet. They are carried in
        # the queue kwargs and unlinked by _dispatch_queued after the turn runs
        # (deleting them now dropped the images silently: p.is_file() was False
        # by dispatch time, so _send_prompt skipped them with no error).
        return
    elif orch.sessions and orch.sessions.enqueue(
        session_key,
        msg_ts,
        clean_text,
        channel=channel,
        thread_ts=thread_ts,
        sender_id=sender_id,
        team_id=team_id,
        agent_override=agent_override,
        user_display_name=_sender_display,
        image_temp_paths=list(_image_temp_paths),
    ):
        logger.info("Message %s queued for busy session %s", msg_ts, session_key)
        if orch.slack:
            try:
                await orch.slack.add_reaction(channel, msg_ts, "hourglass_flowing_sand")
            except Exception:
                logger.debug("Failed to add queue reaction", exc_info=True)
        # See the force=True branch above: cleanup is deferred to
        # _dispatch_queued so the queued turn's clean_text can still resolve
        # its image temp-file paths.
        return

    # ── New transport path: route to the messaging abstraction ──
    # When messaging.use_transport is True, drive the turn through
    # SlackTransport → TurnDriver → SlackRenderer instead of the native
    # inline handle_message loop. Default ON in this fork: MessagingConfig
    # and the loader both default use_transport to True and orch._cfg.messaging
    # is always populated (default_factory), so every install takes this path
    # unless it explicitly sets messaging.use_transport=false to opt back into
    # the native path. (KiroCrew has no challenge-redirect path, so this simply
    # replaces the native dispatch when the flag is on.)
    #
    # Review-mode channels are EXCLUDED from the transport path: review mode is
    # a privacy gate (suppress public streaming/output, post an ephemeral draft
    # with approve/edit/cancel for owner sign-off). That machinery lives only in
    # native handle_message; routing review-mode channels through native keeps
    # that guarantee intact rather than risking a partial re-implementation.
    _use_transport = (
        getattr(getattr(orch._cfg, "messaging", None), "use_transport", False) is True
        and activation != ACTIVATION_REVIEW
    )
    if _use_transport:
        t = asyncio.create_task(
            handle_message_transport(
                orch.slack,  # type: ignore[arg-type]
                orch.sessions,  # type: ignore[arg-type]
                channel,
                clean_text,
                thread_ts,
                msg_ts,
                sender_id,
                context_builder=orch.ctx_builder,
                conversation_log=orch.conv_log,
                # Same approval gating as the native path: respects the
                # configured mode + operator YOLO/SafetyOverride TTL, rather
                # than an unconditional auto-approve. Deny-by-default unless
                # auto-approve is explicitly active.
                approval_mode=_resolve_approval_mode(orch),
                # Per-channel agent override (slack.channels.<id>.agent), same
                # as native handle_message's channel_agent, so a channel-pinned
                # agent is honored on the transport path too.
                agent_override=agent_override,
                # Keyword-command services, same as native handle_message, so
                # `sessions`/`spawn`/`run`/`cron` work on the transport path via
                # the shared maybe_handle_keyword_command interceptor.
                subagent_manager=orch.subagent_mgr,
                task_runner=orch.task_runner,
                cron_service=orch.cron_svc,
                # Respect the user's phase-reaction setting, same as native
                # handle_message — live-read per message, NOT orch._cfg (which
                # is captured at startup and would make toggle saves inert
                # until restart).
                reactions_enabled=KiroCrewConfig.load().slack.reactions_enabled,
                # Respect slack.show_thinking (surface reasoning as a 💭 reply).
                show_thinking=KiroCrewConfig.load().slack.show_thinking,
                # History consolidation + display-name context, same as native
                # handle_message (parity: don't drop these on the transport path).
                consolidator=orch.consolidator,
                user_display_name=_sender_display,
            )
        )
        orch._session_tasks[session_key] = t

        def _on_transport_done(task: asyncio.Task) -> None:  # type: ignore[type-arg]
            orch._handler_tasks.discard(task)
            if orch._session_tasks.get(session_key) is task:
                del orch._session_tasks[session_key]
            _cleanup_image_temps()
            # Drain queue: only if no other task took over this session.
            # Mirrors native _on_done so messages queued while this session was
            # busy aren't stranded when the transport path is the active route.
            try:
                if session_key not in orch._session_tasks and orch.sessions:
                    _next = orch.sessions.dequeue(session_key)
                    # Fall back to orchestrator-level pending queue (pre-session).
                    if not _next:
                        _pq = orch._pending_queue.get(session_key)
                        if _pq:
                            _next = _pq.pop(0)
                            if not _pq:
                                del orch._pending_queue[session_key]
                    if _next:
                        _q_ts, _q_text, _q_kw = _next
                        _q_t = asyncio.ensure_future(
                            _dispatch_queued(orch, session_key, _q_ts, _q_text, _q_kw)
                        )
                        orch._session_tasks[session_key] = _q_t
                        orch._handler_tasks.add(_q_t)
                        _q_t.add_done_callback(_on_transport_done)
            except Exception:
                logger.exception("_on_transport_done drain failed for %s", session_key)

        t.add_done_callback(_on_transport_done)
        orch._handler_tasks.add(t)
        return

    try:
        t = asyncio.create_task(
            handle_message(
                orch.slack,  # type: ignore[arg-type]
                orch.sessions,  # type: ignore[arg-type]
                channel,
                clean_text,
                thread_ts,
                msg_ts,
                sender_id,
                team_id=team_id,
                approval_mode=_resolve_approval_mode(orch),
                context_builder=orch.ctx_builder,
                cron_service=orch.cron_svc,
                conversation_log=orch.conv_log,
                consolidator=orch.consolidator,
                subagent_manager=orch.subagent_mgr,
                task_runner=orch.task_runner,
                channel_agent=agent_override,
                user_display_name=_sender_display,
                from_trusted_bot=from_trusted_bot,
                channel_activation=activation,
                had_voice_input=_had_voice_input,
            )
        )
    except Exception:
        logger.exception("Failed to create handle_message task")
        _cleanup_image_temps()
        return

    orch._session_tasks[session_key] = t

    def _on_done(task: asyncio.Task) -> None:  # type: ignore[type-arg]
        orch._handler_tasks.discard(task)
        if orch._session_tasks.get(session_key) is task:
            del orch._session_tasks[session_key]
        _cleanup_image_temps()
        # Drain queue: only if no other task took over this session
        try:
            if session_key not in orch._session_tasks and orch.sessions:
                _next = orch.sessions.dequeue(session_key)
                # Fall back to orchestrator-level pending queue (pre-session messages)
                if not _next:
                    _pq = orch._pending_queue.get(session_key)
                    if _pq:
                        _next = _pq.pop(0)
                        if not _pq:
                            del orch._pending_queue[session_key]
                if _next:
                    _q_ts, _q_text, _q_kw = _next
                    _q_t = asyncio.ensure_future(
                        _dispatch_queued(orch, session_key, _q_ts, _q_text, _q_kw)
                    )
                    orch._session_tasks[session_key] = _q_t
                    orch._handler_tasks.add(_q_t)
                    _q_t.add_done_callback(_on_done)
        except Exception:
            logger.exception("_on_done drain failed for %s", session_key)

    orch._handler_tasks.add(t)
    t.add_done_callback(_on_done)
