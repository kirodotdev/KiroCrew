"""The agent surface of a code channel: what the bound agent and its people can do.

Three parts, each in its own module, wired here:

* :mod:`.tools` -- the MCP tools the bound session calls (views, plan canvas,
  context bar, rename, status, wrap-up), every one PINNED to the calling
  session's own channel: the channel is resolved from the caller's verified
  session identity, never from an argument.
* :mod:`.commands` -- the channel's runtime slash commands (``/summarize``,
  ``/wrap-up``, and ``/create-pr`` when the session can open a pull request),
  registered with ``agents.conversations.setCommands`` and delivered over
  Socket Mode.
* :mod:`.wrapup` -- the wrap-up summary with its Archive button, and the
  archive that follows a click.
* :mod:`.agent_picker` -- the kickoff's agent picker and ``/kirocrew agent``,
  which switch the channel's session through the dashboard's agent switch.

Everything here is inert while the code-channel gate
(:func:`.binding.code_channels_on`) is shut: the hooks fire only for a bound
channel with the feature on, the slash and button handlers answer "not mine",
and the tools refuse.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import Callable
from typing import Any

from kiro_crew.sel import sel
from kiro_crew.slack.code_channels.store import Binding, store
from kiro_crew.slack.code_channels.surface_client import SurfaceClient

logger = logging.getLogger(__name__)

_slack_provider: Callable[[], Any] | None = None
_INSTALLED = False


def install(slack_provider: Callable[[], Any]) -> None:
    """Subscribe the command and wrap-up hooks (idempotent; called by the core)."""
    global _INSTALLED, _slack_provider
    _slack_provider = slack_provider
    if _INSTALLED:
        return
    from kiro_crew.slack.code_channels import commands, hooks, wrapup

    hooks.on_turn_start(commands.on_turn_start)
    # First: a /wrap-up arriving while a turn starts must see that turn as earlier.
    hooks.on_turn_start(wrapup.on_turn_start, first=True)
    # Before every other start hook: the new turn's stop mark is set first.
    hooks.on_turn_start(note_turn_start, first=True)
    hooks.on_turn_end(wrapup.on_turn_end)
    _INSTALLED = True


def set_slack_provider(provider: Callable[[], Any] | None) -> None:
    """Where the live Slack client comes from (set by :func:`install`; tests)."""
    global _slack_provider
    _slack_provider = provider


def slack() -> Any | None:
    """The live Slack client, or None before Slack connects."""
    try:
        return _slack_provider() if _slack_provider is not None else None
    except Exception:
        return None


def client() -> SurfaceClient | None:
    """The surface's ``agents.*`` client over the live Slack client."""
    live = slack()
    found = SurfaceClient.from_slack(live) if live is not None else None
    return found if isinstance(found, SurfaceClient) else None


def dashboard_state() -> Any | None:
    from kiro_crew.slack.handler import get_dashboard_state

    return get_dashboard_state()


def audit(caller: str, operation: str, outcome: str, resources: str, error: str = "") -> None:
    """One SEL row per decision; a failed write never changes the decision."""
    try:
        sel().log_api_access(
            caller=caller or "unknown",
            operation=operation,
            outcome=outcome,
            source="slack",
            resources=resources,
            error=error,
        )
    except Exception:
        logger.debug("code channel surface SEL write failed", exc_info=True)


def caller_slot_key(state: Any, session_key: str) -> str:
    """The dashboard slot the verified *session_key* belongs to ("" when none)."""
    if state is None or not session_key:
        return ""
    from kiro_crew.dashboard.session_control_parts.caller_access import caller_slot_key as _resolve

    try:
        return _resolve(state, session_key)
    except Exception:
        logger.debug("caller slot resolution failed", exc_info=True)
        return ""


async def binding_for_slot(state: Any, slot_key: str) -> Binding | None:
    """The code channel bound to *slot_key*, when that binding still names it.

    A binding is the caller's only when its session key IS the slot AND the
    channel's Slack link still resolves to that slot: a channel whose session was
    closed and replaced is bound to the replacement, never to the old slot.
    """
    if not slot_key:
        return None
    from kiro_crew.slack.code_channels.binding import bound_slot

    for binding in await asyncio.to_thread(store().bindings):
        if binding.session_key != slot_key:
            continue
        slot = bound_slot(state, binding)
        if slot is None or getattr(slot, "key", "") != slot_key:
            continue
        linked = getattr(slot, "_slack_channel", None)
        if isinstance(linked, str) and linked and linked != binding.channel_id:
            continue  # the anchor ts belongs to some other channel's link
        return binding
    return None


#: Channels this process archived, fenced before (and regardless of) the stamp.
_archived_here: set[str] = set()


def fence_archived(channel_id: str) -> None:
    """Treat *channel_id* as archived for the rest of this process."""
    _archived_here.add(channel_id)


def fenced_here(channel_id: str) -> bool:
    """Whether this process archived *channel_id* (stamp written or not)."""
    return channel_id in _archived_here


def unfence(channel_id: str) -> None:
    """The stamp is written: the stored binding is the answer again (an unarchive clears it)."""
    _archived_here.discard(channel_id)


async def is_archived(binding: Binding) -> bool:
    """Whether the channel is archived: fenced here, or stamped on the stored binding."""
    if binding.channel_id in _archived_here:
        return True
    current = await asyncio.to_thread(store().get_binding, binding.channel_id)
    return bool((current or binding).archived_at)


async def fenced_unrecorded(binding: Binding) -> bool:
    """Archived by this process, but the stamp is not on the stored binding yet."""
    if binding.channel_id not in _archived_here:
        return False
    current = await asyncio.to_thread(store().get_binding, binding.channel_id)
    return not (current or binding).archived_at


def workspace_admitted(team_id: str) -> bool:
    """The per-message workspace gate every inbound Slack path applies (fail closed)."""
    if not team_id:
        return False
    try:
        from kiro_crew.platform import current_context

        return bool(current_context().slack_gate.check_message_origin(team_id))
    except Exception:
        logger.debug("workspace origin check failed", exc_info=True)
        return False


async def outbound_permitted() -> bool:
    """Whether the ``channels`` policy lets this gateway write to Slack."""
    from kiro_crew.messaging.identity import channel_outbound_permitted

    return await channel_outbound_permitted("slack")


def _caller_slack_permitted_sync(caller: str) -> bool:
    from kiro_crew.platform.context import PlatformCompositionError
    from kiro_crew.platform.governance_profiles import governance_permits

    try:
        decision = governance_permits(
            "channels", "slack", session_key=caller, log_warning=False, fail_closed=True
        )
        permitted = bool(getattr(decision, "permitted", False))
    except PlatformCompositionError:
        raise
    except Exception:
        logger.warning("code channel: caller Slack policy unreadable", exc_info=True)
        permitted, decision = False, None
    if not permitted:
        try:
            sel().log_governance_decision(
                session_key=caller,
                tool_name="code_channel:slack",
                scope="channels",
                item="slack",
                outcome="denied",
                rule=getattr(decision, "rule", ""),
                layer=getattr(decision, "layer", ""),
                reason=getattr(decision, "reason", "") or "policy unreadable",
            )
        except Exception:
            logger.debug("code channel caller deny audit failed", exc_info=True)
    return permitted


async def caller_slack_permitted(caller: str) -> bool:
    """Whether the ``channels`` policy lets the CALLING session use Slack (fail closed).

    :func:`outbound_permitted` asks for the gateway (host) profile; a tool call
    acts for the session that asked for it, so its own profile must admit Slack
    too. Off the loop, on the governance executor (profile file I/O).
    """
    from kiro_crew.executors import governance_executor

    return await asyncio.get_running_loop().run_in_executor(
        governance_executor(), _caller_slack_permitted_sync, caller
    )


def channel_policy_refusal(channel_id: str, *, operation: str, caller: str) -> str:
    """Why the channel policy refuses *channel_id* (``""`` when it admits it).

    The per-channel ceiling (``allowed_channel_ids``, tightest wins) every
    inbound path and write applies; audited by the policy seam. Fails closed:
    a policy that can't be read refuses.
    """
    from kiro_crew.slack.channel_policy import channel_refusal

    try:
        return channel_refusal(channel_id, operation=operation, caller=caller or "slack")
    except Exception:
        logger.warning("code channel %s: channel policy unreadable", channel_id, exc_info=True)
        return "channel policy unreadable"


def policy_gated_handlers() -> tuple[tuple[Any, int], ...]:
    """Every Slack action/interaction handler of the code channel, with how many
    :func:`channel_policy_refusal` calls it makes: one before any side effect and
    one at its commit point, after the awaits a policy change can land during.

    ``tools.run_tool`` checks once, after its own awaits and just before the tool
    runs; ``commands.sync_commands`` at entry and again after its lock and read,
    before the ``gh`` probe. Every Slack write after that meets the
    policy again in the clients (``SurfaceClient._call`` for ``agents.*``,
    ``SlackClient``'s ``require_channel`` for ``chat.*``). A slash command's turn
    runs through ``routing.deliver_directed``, which checks before a session is
    bound and again right before the turn (whose admission prepares the
    worktree). ``_bind_channel`` (also reached by a new channel's creation)
    checks before it cuts the worktree and before it binds the session. The
    only writes outside both clients are a slash
    command's fixed-text replies to its own invoker (``response_url``). A new
    handler belongs on this list; its test
    (``test_every_policy_gated_handler_checks_at_entry_and_at_its_commit_point``)
    holds the counts.
    """
    from kiro_crew.slack.code_channels import commands, routing, tools, wrapup

    return (
        (commands.handle_slash, 2),  # /summarize, /wrap-up, /create-pr
        (routing.deliver_directed, 2),  # the bind, then the turn a command runs
        (routing._refuse_bind_if_policy_refuses, 1),  # in _bind_channel: cut, then session
        (wrapup.handle_archive_click, 3),  # the Archive button (and its recovery)
        (wrapup._post_wrap_up, 2),  # the wrap-up message (tool and /wrap-up fallback)
        (tools._run_tool, 1),  # every agent tool, incl. the context bar
        (commands.sync_commands, 2),  # the channel's command set, at turn start
    )


def channel_admits(cfg: Any, binding: Binding) -> bool:
    """The code channel and its origin admit the bot (off or review mode refuse).

    The same rule the message path and the slash commands apply, so an operator
    who turns the bot off in either is obeyed by the agent's own writes too.
    """
    from kiro_crew.slack.code_channels.routing import _origin_of, channel_and_origin_permit

    channel = binding.channel_id
    return channel_and_origin_permit(cfg, channel, _origin_of(channel, binding, None))


# ── The Stop fence ────────────────────────────────────────────────────────
#
# One fence for every Stop of a code channel (Slack's native Stop and ``!stop``
# both go through ``routing._stop_bound``, which calls :func:`mark_stopped`).
# A stop gets the next number of one process-wide sequence. A turn records the
# sequence at its start, and a tool call or wrap-up records it when admitted
# (:func:`admit`, a context variable that every await below it inherits). Work
# admitted before a later stop of its channel writes nothing more to Slack:
# ``SurfaceClient._call`` re-checks right before every send.

_stop_seq = 0
_stopped_at: dict[str, int] = {}
_turn_started_at: dict[str, int] = {}
_ADMITTED: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "code_channel_admitted", default=None
)


def mark_stopped(channel_id: str) -> None:
    """A person stopped *channel_id*: work admitted before now writes nothing."""
    global _stop_seq
    _stop_seq += 1
    _stopped_at[channel_id] = _stop_seq


def _stopped_since(channel_id: str, mark: int) -> bool:
    return _stopped_at.get(channel_id, 0) > mark


def admit() -> contextvars.Token[int | None]:
    """Admit the work in this context at the current stop sequence.

    Nested admissions keep the outer mark, so a stop between an outer and an
    inner admission is never forgotten. Pass the token to :func:`release`.
    """
    current = _ADMITTED.get()
    return _ADMITTED.set(_stop_seq if current is None else current)


def release(token: contextvars.Token[int | None]) -> None:
    _ADMITTED.reset(token)


def admitted_work_stopped(channel_id: str) -> bool:
    """Whether the work this context admitted was stopped since, for *channel_id*."""
    mark = _ADMITTED.get()
    return mark is not None and _stopped_since(channel_id, mark)


def commit_if_not_stopped(channel_id: str) -> bool:
    """The one commit point: whether the work in this context may commit now.

    Called synchronously right before every side effect that follows an await
    (a Slack send, a store or record write, a status hold), with no await
    between it and the write. Admitted work commits only while its channel is
    not stopped; work nobody admitted (a person's click, a background pass) is
    never fenced. ``test_slack_code_channel_commit_point.py`` holds every
    handler module to it.
    """
    if _ADMITTED.get() is None:
        return True
    return not channel_stopped(channel_id)


async def note_turn_start(binding: Binding, _event: Any) -> None:
    """A turn hook: the channel's new turn is not stopped by an earlier Stop."""
    _turn_started_at[binding.channel_id] = _stop_seq


def channel_stopped(channel_id: str) -> bool:
    """Whether Slack's Stop silenced a turn of *channel_id* that is still unwinding.

    Slack's rule after ``agent_session_stopped``: no further posts and no
    ``agents.*`` calls for that context. True when the stop fence says the
    current turn or the admitted work was stopped, or a thread-session fence
    scope for the channel is stopped.
    """
    if admitted_work_stopped(channel_id):
        return True
    started = _turn_started_at.get(channel_id)
    if started is not None and _stopped_since(channel_id, started):
        return True
    try:
        from kiro_crew.slack.agent_sessions import fence

        live = getattr(fence, "_LIVE", ())
        return any(s.channel == channel_id and s.stopped for s in list(live))
    except Exception:
        return False


def push_slots(state: Any) -> None:
    push = getattr(state, "push_slots_update", None)
    if callable(push):
        try:
            push()
        except Exception:
            logger.debug("slots push failed", exc_info=True)
