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
* :mod:`.actions` -- the context bar's Create PR and Stop buttons
  (``code_channel_action``), each running the path its command or button runs.
* :mod:`.comments` -- plan-canvas comments read back into the session.
* :mod:`.views` -- the view tabs at Slack's five-tab cap.
* :mod:`.tabs` -- the channel's own tabs: native pull-request tabs and the
  interactive Workers tab; :mod:`.worker_stop` answers its Stop button.

Everything here is inert while the code-channel gate
(:func:`.binding.code_channels_on`) is shut: the hooks fire only for a bound
channel with the feature on, the slash and button handlers answer "not mine",
and the tools refuse.
"""

from __future__ import annotations

import asyncio
import logging
import re
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
    from kiro_crew.slack.code_channels import commands, comments, hooks, wrapup

    hooks.on_turn_start(commands.on_turn_start)
    # First: a /wrap-up arriving while a turn starts must see that turn as earlier.
    hooks.on_turn_start(wrapup.on_turn_start, first=True)
    hooks.on_turn_end(wrapup.on_turn_end)
    hooks.on_turn_end(comments.on_turn_end)
    comments.start_poll()
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


_SLACK_USER_ID = re.compile(r"[UW][A-Z0-9]+")


def owner_name() -> str:
    """Who "the owner" is, for a refusal: ``the Kiro Crew owner (<@U…>)``.

    The owner is this gateway's configured Slack owner (``slack.owner_id``), the
    one ``is_owner`` checks; a reader can't otherwise tell whether a refusal means
    the channel's, the workspace's or the app's owner.
    """
    try:
        from kiro_crew.slack import handler

        owner = str(getattr(handler, "_owner_id", "") or "")
    except Exception:  # pragma: no cover - the plain wording still holds
        owner = ""
    # Only a well-formed Slack user id becomes a mention: anything else could
    # break out of ``<@…>`` and inject formatting.
    if not _SLACK_USER_ID.fullmatch(owner):
        return "the Kiro Crew owner"
    return f"the Kiro Crew owner (<@{owner}>)"


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


def channel_policy_refusal(
    channel_id: str, *, operation: str, caller: str, audit: bool = True
) -> str:
    """Why the channel policy refuses *channel_id* (``""`` when it admits it).

    The per-channel ceiling (``allowed_channel_ids``, tightest wins) every
    inbound path and write applies; audited by the policy seam unless *audit* is
    False (a caller that records the refusal itself). Fails closed: a policy that
    can't be read refuses.
    """
    from kiro_crew.slack.channel_policy import channel_refusal

    try:
        return channel_refusal(
            channel_id, operation=operation, caller=caller or "slack", audit=audit
        )
    except Exception:
        logger.warning("code channel %s: channel policy unreadable", channel_id, exc_info=True)
        return "channel policy unreadable"


def display_text(text: str, *, mentions: bool = True) -> str:
    """Text for a code-channel surface, through the shared outbound display sink.

    The only ``display_safe_for`` call in a code channel (``wrapup`` delegates
    here), so code-channel text is never sanitized two ways: control comments
    stripped, display-form credential and exfiltration redaction through the
    active platform context (an installation's own patterns apply), and, with
    *mentions*, Slack's mention grammar defanged. ``mentions=False`` is for text
    no Slack message parses (a dashboard transcript row).
    """
    from dataclasses import replace

    from kiro_crew.messaging.renderer import display_safe_for
    from kiro_crew.platform.context import redact_via_context
    from kiro_crew.slack.transport import SLACK_CAPABILITIES

    capabilities = (
        SLACK_CAPABILITIES if mentions else replace(SLACK_CAPABILITIES, mention_grammars=False)
    )
    return display_safe_for(text, capabilities, redactor=redact_via_context)


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
    bound, before the worktree and again before the turn. ``_bind_channel`` (also
    reached by a new channel's creation) checks before it cuts the worktree and
    before it binds the session; its closing ``prepare_workspace`` cuts nothing
    new and publishes only through the policy-checked client. The only writes outside both clients are a slash
    command's fixed-text replies to its own invoker (``response_url``). A new
    handler belongs on this list; its test
    (``test_every_policy_gated_handler_checks_at_entry_and_at_its_commit_point``)
    holds the counts.

    The owner controls (the agent picker's select and ``/kirocrew agent``, the
    Approval mode select and ``/kirocrew trust``, "Trust this channel", a worker
    card's buttons) all enter through ``agent_picker.authorize`` and commit
    through ``agent_picker._sync_gates``, so those two carry the guard for all of
    them; ``test_every_owner_control_meets_the_policy_at_entry_and_commit``
    holds that every control reaches both. Their routers
    (``interactions.dispatch``, ``worker_approvals.dispatch_card_click``) pair the
    ``channels`` gate with ``channel_refusal``, the inbound-gate ratchet's form
    (``test_every_slack_inbound_gate_also_asks_the_channel`` holds them), so they
    are not counted here. A change to Normal and a worker card's Reject only take
    a grant away: Normal is never held back at its commit, a Reject is.
    """
    from kiro_crew.slack.code_channels import (
        actions,
        agent_picker,
        commands,
        routing,
        tools,
        worker_approvals,
        worker_stop,
        wrapup,
    )

    return (
        (agent_picker.authorize, 1),  # every owner control's entry
        (agent_picker._sync_gates, 1),  # every owner control's commit fence
        (worker_approvals.handle_click, 1),  # a worker card's Reject at its commit
        (worker_approvals.relay, 1),  # a worker's card, right before it posts
        # /summarize, /wrap-up, /create-pr and the bar's Create PR (handle_slash and
        # the action both run their gates and effects through run_command)
        (commands.run_command, 2),
        (routing.deliver_directed, 3),  # bind, worktree, then the turn a command runs
        (routing._refuse_bind_if_policy_refuses, 1),  # in _bind_channel: cut, then session
        (wrapup.handle_archive_click, 3),  # the Archive button (and its recovery)
        (wrapup.post_wrap_up, 2),  # the wrap-up message (tool and /wrap-up fallback)
        (tools.run_tool, 1),  # every agent tool, incl. the context bar
        (commands.sync_commands, 2),  # the channel's command set, at turn start
        # Every channel button's entry (the context bar's and the Workers tab's)
        (actions.admit, 1),
        # The Workers tab's Stop: its last await, then its synchronous final gate
        (worker_stop._handle, 1),
        (worker_stop._channel_still_admitted, 1),
    )


async def write_refusal(
    channel_id: str,
    binding: Binding | None = None,
    *,
    activation: bool = False,
    scope: Callable[[], bool] | None = None,
) -> str:
    """THE write gate for this package's Slack writes: ``""`` when one may go out NOW.

    Otherwise why not: ``channels_policy`` (the ``channels`` governance policy
    denies Slack), ``archived``, ``stopped`` (Slack's own Stop: no ``agents.*``
    call or post until the channel's next turn), with *activation* (needs
    *binding*) ``rebound`` (the channel is bound to another session now) or
    ``channel_off`` (code channels are off, the code channel or its origin is
    Off or in Review, or the config cannot be read), or
    ``out_of_scope`` (*scope*, the board's scope check for a write that carries
    worker content).

    Its one await, the policy read, comes FIRST and every other condition is read
    after it synchronously, so a caller that sends right after awaiting this has
    no await between the decision and the write: nothing that changes meanwhile
    (a policy deny, an archive, a Stop, an Off) can slip through.
    """
    if not await outbound_permitted():
        return "channels_policy"
    return write_refusal_now(channel_id, binding, activation=activation, scope=scope)


def write_refusal_now(
    channel_id: str,
    binding: Binding | None = None,
    *,
    activation: bool = False,
    scope: Callable[[], bool] | None = None,
) -> str:
    """:func:`write_refusal` without the policy read (it must run just before). Fails closed."""
    return _fences_now(
        channel_id, binding, stop=True, activation=activation, scope=scope, toggle=activation
    )


async def act_refusal(
    channel_id: str,
    binding: Binding | None,
    *,
    team: str | None = None,
    stop: bool = True,
    scope: Callable[[], bool] | None = None,
) -> str:
    """THE gate for an inbound side effect: ``""`` when it may happen NOW.

    For what this package does on Slack's behalf after an await: starting a
    turn (a canvas comment's feedback, a context-bar click's command), marking
    canvas comments seen, arming a wrap-up. The same fences as
    :func:`write_refusal` with the inbound policy in place of the outbound one:
    ``channels_policy`` (the ``channels`` policy denies inbound Slack),
    ``workspace`` (*team* is not an admitted workspace), ``channel_policy``
    (the per-channel ceiling refuses the channel), ``archived``,
    ``rebound`` (the channel is bound to another session now), ``stopped``
    (with *stop*: Slack's own Stop, for what nobody asked for just now; a
    person's click passes ``stop=False``), ``channel_off`` (code channels are
    off, or the channel or its origin is Off or in Review) or ``out_of_scope``.

    Same shape as :func:`write_refusal`: the one await first, everything else
    read synchronously after it, so the caller acts right after with no await
    between. A refused item is left as it was (not routed, not marked seen).
    """
    from kiro_crew.messaging.identity import channel_inbound_permitted
    from kiro_crew.slack.channel_policy import channel_refusal

    channel = channel_id
    try:
        # The pair every Slack inbound gate makes: the channels ceiling and the
        # per-channel one, read again (its entry gate audited the first read).
        if not (
            await channel_inbound_permitted("slack") and not channel_refusal(channel, audit=False)
        ):
            return "channel_policy" if channel_refusal(channel, audit=False) else "channels_policy"
    except Exception:
        logger.debug("code channel %s: inbound policy read failed", channel_id, exc_info=True)
        return "error"
    try:
        if team is not None and not workspace_admitted(team):
            return "workspace"
    except Exception:
        return "error"
    return _fences_now(channel_id, binding, stop=stop, activation=True, scope=scope, toggle=True)


def _fences_now(
    channel_id: str,
    binding: Binding | None,
    *,
    stop: bool,
    activation: bool,
    scope: Callable[[], bool] | None,
    toggle: bool = False,
) -> str:
    """The synchronous fences both gates read after their one await. Fails closed."""
    try:
        if archived_now(channel_id, binding):
            return "archived"
        if toggle and binding is not None and _rebound(binding):
            return "rebound"
        if stop and channel_stopped(channel_id):
            return "stopped"
        if activation:
            from kiro_crew.slack.code_channels.binding import code_channels_on, live_config

            cfg = live_config()  # read now, after the policy await
            if binding is None or cfg is None:
                return "channel_off"
            if toggle and not code_channels_on(cfg):
                return "channel_off"
            if not channel_admits(cfg, binding):
                return "channel_off"
        if scope is not None and not scope():
            return "out_of_scope"
    except Exception:
        logger.debug("code channel %s: write gate failed", channel_id, exc_info=True)
        return "error"
    return ""


def _rebound(binding: Binding) -> bool:
    """The channel's binding as stored now names another session (or is gone)."""
    loaded = store().loaded_bindings()
    if loaded is None:
        return False  # not loaded: unknown here, as for the archive stamp
    for current in loaded:
        if current.channel_id == binding.channel_id:
            return current.session_key != binding.session_key
    return True


def archived_now(channel_id: str, binding: Binding | None = None) -> bool:
    """Archived by this process, or stamped on the binding as it stands now.

    The stamp is read from the store's loaded bindings (in memory, no disk),
    not only from the caller's copy, which may predate the archive: the
    in-process fence is lifted once the stamp is written.
    """
    if channel_id in _archived_here or (binding is not None and binding.archived_at):
        return True
    for loaded in store().loaded_bindings() or ():
        if loaded.channel_id == channel_id:
            return bool(loaded.archived_at)
    return False


def channel_admits(cfg: Any, binding: Binding) -> bool:
    """The code channel and its origin admit the bot (off or review mode refuse).

    The same rule the message path and the slash commands apply, so an operator
    who turns the bot off in either is obeyed by the agent's own writes too.
    """
    from kiro_crew.slack.code_channels.routing import _origin_of, channel_and_origin_permit

    channel = binding.channel_id
    return channel_and_origin_permit(cfg, channel, _origin_of(channel, binding, None))


def channel_stopped(channel_id: str) -> bool:
    """Whether Slack's Stop silenced a turn of *channel_id* that is still unwinding.

    Slack's rule after ``agent_session_stopped``: no further posts and no
    ``agents.*`` calls for that context. True as well from Slack's own Stop of
    the whole channel until the next turn starts (:func:`.status.is_quiet`).
    """
    try:
        from kiro_crew.slack.code_channels.status import is_quiet

        if is_quiet(channel_id):
            return True
    except Exception:
        return False
    return turn_unwinding(channel_id)


def turn_unwinding(channel_id: str) -> bool:
    """Whether a turn of *channel_id* that Slack's Stop stopped is still unwinding.

    Narrower than :func:`channel_stopped`: false again once that turn is gone,
    even before the channel's next turn. For work that goes on after the
    channel session's Stop and waits on a person (a worker's tool approval): a
    card it needs must still reach the channel, or the worker waits unseen.
    """
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
