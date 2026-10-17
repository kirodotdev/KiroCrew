"""A dashboard turn's spec hooks and agent notices: reading the hooks the session's agent spec
declares, and the welcome, run-nowhere and withheld-hooks notice rows."""

from __future__ import annotations

import asyncio
import functools
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        ACP_BACKEND_KAS,
        SPEC_HOOKS_NOTICE_KIND,
        DashboardState,
        _ChatSlot,
        agent_welcome_message,
        append_and_surface,
        capabilities_of,
        crew_fired_spec_hooks,
        logger,
        session_agent,
        spec_hook_events,
        spec_hooks_run_nowhere,
        spec_project_dir,
        withheld_project_spec_hooks,
    )
    from kiro_crew.dashboard.chat_turn.deny_chokepoints import _redact_display_text
    from kiro_crew.dashboard.chat_turn.tool_approval import (
        _spec_confirm_hooks_notice,
        _spec_hooks_run_nowhere_notice,
        _spec_keys_notice,
        _spec_withheld_hooks_notice,
    )


async def _surface_agent_welcome(
    state: "DashboardState",
    slot: "_ChatSlot",
    agent: str,
) -> None:
    """Render *agent*'s ``welcomeMessage`` into *slot*, at most once per activation.

    The one consumer of the agent-config field. An agent JSON may carry a
    ``welcomeMessage`` — a usage hint its author wants the user to read when the
    agent takes over — and this is the only place it reaches a user. Kiro Crew's
    own bundled agents ship one, so the field is not a niche extension point.

    Three properties are the whole design, and each one rules out an
    alternative that looks simpler:

    * **A transcript row, not prompt context.** Role ``notice``: durable (not in
      ``_TRANSIENT_ROLES``, so it persists and survives a reload) but NOT one of
      the ``user``/``assistant`` roles the history replay feeds back to the
      model. Inlining the hint into the system prompt is the workaround the
      issue reports as the thing to remove — it re-sends the text every turn.
    * **One shot per activation.** Both call sites clear through
      ``slot._welcomed_agent``, because a switch and the session start its own
      reset produces are two events for ONE activation, and each path would
      otherwise emit for the other.
    * **Untrusted text.** The field is read from a user-writable, tool-shared
      directory. ``NoticeCard`` renders a ``notice`` row as plain text (no
      markdown, no HTML), the reader caps its length, and it is passed through
      the same display redactors as any other foreign string reaching a
      transcript — so a hint carrying a credential or an exfil URL cannot ship
      one into the persisted window.

    Best-effort: the disk read is offloaded (this runs on the gateway's shared
    event loop) and a failure renders nothing rather than failing the turn.
    """
    if not agent:
        # The default crew has no spec, so there is nothing to render — but the
        # claim is RELEASED rather than merely skipped: leaving it set made
        # A -> default -> A read as "already welcomed A" and swallow A's second
        # activation, which the one-shot rule is per activation, not per slot.
        slot._welcomed_agent = ""
        return
    if slot._welcomed_agent == agent:
        return
    # Claimed BEFORE the await: two events for one activation can both reach
    # this point, and the offload yields the loop between the guard and the
    # append. Recording the failure case as "welcomed" too is deliberate — a
    # hint that could not be read is not worth retrying on every later event
    # for the same agent.
    slot._welcomed_agent = agent
    try:
        text = await asyncio.to_thread(agent_welcome_message, agent, project=slot.project or None)
    except Exception:  # noqa: BLE001 - decoration must never fail a turn
        logger.debug("Failed to read welcomeMessage for agent %r", agent, exc_info=True)
        return
    if not text:
        return
    append_and_surface(state, slot, "notice", _redact_display_text(text), "msg msg-info")


async def _prepare_spec_hooks(
    state: "DashboardState",
    slot: "_ChatSlot",
    client: Any,
    agent: str,
    *,
    is_new: bool,
) -> tuple[list, bool, str | None]:
    """This turn's spec hooks for ``_fire``, whether they could not be read, and
    the session workspace they run in (``None`` for the gateway's own).

    Only for a backend whose capabilities say Crew fires the spec's hooks: kiro-cli
    runs the field itself, and firing it here too would run every hook twice, so
    every other session gets ``([], False, None)``.

    On a new session, a spec that sets a key nothing carries to this backend gets
    one notice row, and so does a spec with ``confirm: true`` hooks, which Crew
    skips because it cannot ask for the confirmation. A new session on a backend
    where nothing runs the spec's hooks (``spec_hooks_run_nowhere``) gets one
    notice row naming their events. None of them runs silently.
    """
    if not capabilities_of(client).crew_fires_spec_hooks:
        if is_new and spec_hooks_run_nowhere(client):
            await _notice_spec_hooks_run_nowhere(state, slot, client, agent)
        return [], False, None
    cwd = getattr(client, "cwd", "")
    work_dir = cwd if isinstance(cwd, str) and cwd else None
    agent = session_agent(client, agent)
    if not agent:
        # Whose hooks apply is unknown, so PreToolUse fails closed.
        logger.warning("no agent is known for this KAS session; tool calls are blocked")
        return [], True, work_dir
    try:
        hooks, lost, unconfirmable = await asyncio.to_thread(
            functools.partial(
                crew_fired_spec_hooks,
                agent,
                spec_project_dir(client),
                # Only KAS receives `toolsSettings` as `permissions` rules.
                translates_tools_settings=capabilities_of(client).backend == ACP_BACKEND_KAS,
            )
        )
    except Exception:  # noqa: BLE001 - the caller fails PreToolUse closed
        logger.warning(
            "agent spec hooks for %r could not be read; tool calls are blocked",
            agent,
            exc_info=True,
        )
        return [], True, work_dir
    if is_new and lost:
        append_and_surface(
            state,
            slot,
            "notice",
            _redact_display_text(_spec_keys_notice(agent, lost)),
            "msg msg-info",
        )
    if is_new and unconfirmable:
        append_and_surface(
            state,
            slot,
            "notice",
            _redact_display_text(_spec_confirm_hooks_notice(agent, unconfirmable)),
            "msg msg-info",
        )
    if is_new:
        # A withheld project spec yields the user-level spec in its place, so
        # these hooks are exactly the user-level ones that still run.
        await _surface_withheld_project_hooks(
            state, slot, client, agent, user_hooks_run=bool(hooks)
        )
    return hooks, False, work_dir


async def _notice_spec_hooks_run_nowhere(
    state: "DashboardState", slot: "_ChatSlot", client: Any, agent: str
) -> None:
    """One notice row naming the spec hook events a session on *client* never runs.

    Best-effort: nothing is gated on these hooks, so a spec that cannot be read
    renders no row rather than failing the turn.
    """
    agent = session_agent(client, agent)
    if not agent:
        return
    try:
        events = await asyncio.to_thread(spec_hook_events, agent, spec_project_dir(client))
    except Exception:  # noqa: BLE001 - an informational row must never fail a turn
        logger.debug(
            "agent spec hooks for %r could not be read for the notice", agent, exc_info=True
        )
        return
    if events:
        append_and_surface(
            state,
            slot,
            "notice",
            _redact_display_text(_spec_hooks_run_nowhere_notice(agent, events)),
            "msg msg-info",
            meta={"kind": SPEC_HOOKS_NOTICE_KIND},
        )


async def _surface_withheld_project_hooks(
    state: "DashboardState",
    slot: "_ChatSlot",
    client: Any,
    agent: str,
    *,
    user_hooks_run: bool,
) -> None:
    """One notice row when the checkout's own spec sets ``hooks`` Crew does not run.

    The hooks are withheld by :func:`trusted_project_agent_spec`; this only says so,
    so a repository's hooks never stop running unseen. *user_hooks_run* says
    whether the user-level spec read in its place carries hooks this session runs,
    which the notice states only when true. Best-effort: a failed check renders
    nothing rather than failing the turn.
    """
    project_dir = spec_project_dir(client)
    if not project_dir:
        return
    try:
        withheld = await asyncio.to_thread(withheld_project_spec_hooks, agent, project_dir)
    except Exception:  # noqa: BLE001 - a notice must never fail a turn
        logger.debug("could not check %r's project spec for withheld hooks", agent, exc_info=True)
        return
    if withheld:
        append_and_surface(
            state,
            slot,
            "notice",
            _redact_display_text(_spec_withheld_hooks_notice(agent, user_hooks_run=user_hooks_run)),
            "msg msg-info",
            meta={"kind": SPEC_HOOKS_NOTICE_KIND},
        )
