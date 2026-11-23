"""A KAS project-only agent's own ``hooks``: whether they run, and the notice state.

Split out of :mod:`kiro_crew.agent_sdk.spec_hooks`, which re-exports the names its
callers import from there. On a ``cron:`` key, whether they run follows the kind of
turn the session's batch was projected for: a person's, or a scheduled run's.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, NamedTuple

logger = logging.getLogger("kiro_crew.agent_sdk.spec_hooks")


def _live_bot_owns(agent_id: str) -> bool:
    """Whether a live Bot's own crewmate file answers *agent_id* before the checkout.

    The rule the KAS projection applies (``acp.harness.kas._bot_owns_mate_file``):
    a file a deleted Bot left behind yields to a checkout spec of the same name, so
    its presence alone must not withhold that folder agent's hooks.
    """
    from kiro_crew.acp.kas_agents import agent_spec_absent
    from kiro_crew.config.paths import mate_agents_dir
    from kiro_crew.mate_capabilities import binding_is_mate

    return not agent_spec_absent(mate_agents_dir(), agent_id) and binding_is_mate(agent_id)


def _consented_kas_project_hooks(agent_id: str, checkout: str) -> dict[str, Any] | None:
    """A KAS project-only agent's ``hooks``, when *checkout*'s choice lets them run.

    Mirrors the order KAS resolves the agent in (``acp.harness.kas.resolve_projected_spec``):
    called only when the user level does not declare *agent_id*, and a crewmate's own
    config of that name wins over the checkout's. Only ``hooks`` is handed back, so no
    other key of the checkout's spec (``mcpServers`` among them) reaches a reader here.
    ``None`` when the checkout does not declare the agent or its hooks are off; a
    declared spec that cannot be read raises, so PreToolUse fails closed.
    """
    from kiro_crew.project_hooks_consent import consented_project_hooks

    if _live_bot_owns(agent_id):
        return None
    # The hooks come from the same read the consent was checked against, never a
    # second one a rewrite could slip other hooks into.
    return consented_project_hooks(checkout, agent_id)


class KasProjectHooks(NamedTuple):
    """What the session-start notice says about a KAS folder agent's hooks."""

    #: ``""`` (no notice), ``"off"``, ``"on"`` or ``"kiro_cli"``.
    state: str
    #: The checkout's agent file the hooks come from.
    path: str = ""
    #: ``(event, command)`` for each hook, as Crew would fire it.
    hooks: tuple[tuple[str, str], ...] = ()


def kas_project_hooks_state(agent_id: str, checkout: str | None) -> KasProjectHooks:
    """Whether a KAS session's folder agent carries hooks, whether they run, and which.

    ``state`` is ``""`` when *agent_id* is not a project-only agent of *checkout*
    with non-empty ``hooks`` (the user level or a crewmate config declares it, or
    the folder's spec sets none or cannot be read); else ``"on"`` (the owner turned
    them on in Crew), ``"kiro_cli"`` (they run because kiro-cli trusts the folder),
    or ``"off"``. For the session-start notice, which names the file and lists the
    commands so the owner can read them before turning them on.
    """
    from kiro_crew.acp.kas_agents import agent_spec_absent

    # circular import: spec_hooks re-exports this module.
    from kiro_crew.acp.session_mcp import _project_spec_path_for, project_agent_spec
    from kiro_crew.agent_sdk import spec_hooks
    from kiro_crew.config.paths import spec_dir_for
    from kiro_crew.project_hooks_consent import SOURCE_KIRO_CLI, hooks_enabled

    if not checkout or not agent_spec_absent(spec_dir_for(agent_id), agent_id):
        return KasProjectHooks("")
    if _live_bot_owns(agent_id):
        return KasProjectHooks("")
    declared, spec = project_agent_spec(agent_id, checkout)
    if not declared or not isinstance(spec, dict) or not spec.get("hooks"):
        return KasProjectHooks("")
    path = _project_spec_path_for(agent_id, checkout)
    listed = tuple((h.event, h.command) for h in spec_hooks._convert(agent_id, spec)[0])
    enabled, source = hooks_enabled(checkout)
    if not enabled:
        state = "off"
    else:
        state = "kiro_cli" if source == SOURCE_KIRO_CLI else "on"
    return KasProjectHooks(state, str(path) if path else "", listed)


#: The session-key prefix of a cron turn's session. A scheduled cron turn never runs
#: a KAS folder agent's own hooks (``spec_hooks.turn_spec_hooks``, ``project_hooks=False``).
CRON_SESSION_KEY_PREFIX = "cron:"

#: True while a person's own turn claims its session (the dashboard send path,
#: :func:`person_turn_claim`). A ``cron:``-keyed KAS session projected under it is a
#: person-kind batch, with the folder agent's hooks; any other is scheduled-kind.
PERSON_TURN: ContextVar[bool] = ContextVar("kas_person_turn", default=False)


@contextmanager
def person_turn_claim(person: bool) -> Iterator[None]:
    """Mark the session claims made inside as a person's turn, or not (*person*)."""
    token = PERSON_TURN.set(person)
    try:
        yield
    finally:
        PERSON_TURN.reset(token)


def kas_hook_checkout(provider: object) -> str | None:
    """A KAS session's own folder, the one a project-only agent's hooks may come from.

    ``None`` off KAS: the other backends that Crew fires spec hooks for resolve the
    checkout through ``spec_hooks.spec_project_dir`` instead. ``None`` for a
    ``cron:``-keyed session whose batch was projected for a scheduled turn
    (``kas_folder_hooks_projected``, recorded by ``KasHarness.record_session_projection``),
    so firing and the stale check follow what the batch was built with, a scheduled
    fire's claim-time check included; only a scheduled run's own firing and in-turn
    check use the user-level hooks alone, even on a person-kind batch
    (``cron_service.turn_hooks`` passes ``project_hooks=False``).
    """
    # circular import: spec_hooks re-exports this module.
    from kiro_crew.agent_sdk import spec_hooks
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS

    if spec_hooks.capabilities_of(provider).backend == ACP_BACKEND_KAS:
        if spec_hooks.provider_session_key(provider).startswith(
            CRON_SESSION_KEY_PREFIX
        ) and not getattr(provider, "kas_folder_hooks_projected", False):
            return None
        cwd = getattr(provider, "cwd", "")
        return cwd if isinstance(cwd, str) and cwd else None
    return None


def with_folder_pre_tool_mark(
    custom_agents: list[dict[str, Any]], agent_id: str, spec: dict[str, Any], gated: bool = False
) -> list[dict[str, Any]]:
    """*custom_agents*, *agent_id*'s entry marked when its folder *spec* kept a
    PreToolUse hook Crew fires, or when the batch left out a folder sub-agent target
    for its hooks (*gated*) (``kas_permissions.CREW_FOLDER_PRE_TOOL_KEY``)."""
    # circular import: spec_hooks re-exports this module.
    from kiro_crew.acp.kas_permissions import CREW_FOLDER_PRE_TOOL_KEY
    from kiro_crew.agent_sdk.spec_hooks import spec_script_hooks
    from kiro_crew.hooks import HOOK_EVENT_PRE_TOOL_USE

    if not gated and not any(
        h.enabled and h.event == HOOK_EVENT_PRE_TOOL_USE and h.command.strip()
        for h in spec_script_hooks(agent_id, spec)
    ):
        return custom_agents
    return [
        (
            {**a, CREW_FOLDER_PRE_TOOL_KEY: True}
            if isinstance(a, dict) and a.get("id") == agent_id
            else a
        )
        for a in custom_agents
    ]


def scheduled_turn_reprojects(provider: object) -> bool:
    """Whether a scheduled turn must re-project *provider*'s batch before it runs.

    True for a person-kind ``cron:`` batch that kept the folder PreToolUse hooks of its
    agent or left out a folder sub-agent target for them
    (:func:`with_folder_pre_tool_mark`), so that batch lacks what the scheduled turn
    gets. Read off the batch's own record, never the folder's files now.
    """
    # circular import: spec_hooks re-exports this module.
    from kiro_crew.acp.kas_agents_registered import _registered_entry
    from kiro_crew.acp.kas_permissions import CREW_FOLDER_PRE_TOOL_KEY
    from kiro_crew.agent_sdk import spec_hooks

    key = spec_hooks.provider_session_key(provider)
    if not key.startswith(CRON_SESSION_KEY_PREFIX) or kas_hook_checkout(provider) is None:
        return False
    entry = _registered_entry(
        getattr(provider, "kas_registered_agents", None), spec_hooks.session_agent(provider, "")
    )
    return isinstance(entry, dict) and entry.get(CREW_FOLDER_PRE_TOOL_KEY) is True


async def person_turn_reprojects(
    provider: object, agent_id: str, pending: Callable[[], Awaitable[bool]]
) -> bool:
    """Whether a person's turn re-projects *provider*'s scheduled-kind ``cron:`` batch.

    Only when the folder agent has consented hooks now, or a native sub-agent
    target the batch registered is a folder agent with consented PreToolUse hooks
    now, which a person-kind batch leaves out (a user-level agent of the same name
    never resets; a consented spec that cannot be read re-projects, so the turn
    fails closed), and only while no sub-agent work is *pending* on the session;
    with work pending the turn runs without the folder hooks, logged.
    """
    # circular import: spec_hooks re-exports this module.
    from kiro_crew.agent_sdk import spec_hooks
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS

    key = spec_hooks.provider_session_key(provider)
    if not (
        key.startswith(CRON_SESSION_KEY_PREFIX)
        and spec_hooks.capabilities_of(provider).backend == ACP_BACKEND_KAS
        and not getattr(provider, "kas_folder_hooks_projected", False)
    ):
        return False
    agent_id = spec_hooks.session_agent(provider, agent_id)
    cwd = getattr(provider, "cwd", "")
    # The batch projects its sub-agent targets from the runtime's folder, which a
    # shared runtime keeps apart from the session's (``project_subagent_targets``);
    # an ``AcpProvider`` reaches that runtime through its ``_client``.
    runtime = getattr(provider, "_client", provider)
    target_dir = str(getattr(runtime, "_work_dir", "") or cwd)
    targets = [
        target
        for a in getattr(provider, "kas_registered_agents", None) or ()
        if isinstance(a, dict) and isinstance(target := a.get("id"), str) and target != agent_id
    ]

    def _consented(name: str, folder: str) -> dict[str, Any]:
        from kiro_crew.acp.kas_agents import agent_spec_absent
        from kiro_crew.config.paths import spec_dir_for

        if not agent_spec_absent(spec_dir_for(name), name):
            return {}
        return _consented_kas_project_hooks(name, folder) or {}

    def _folder_hooks() -> bool:
        from kiro_crew.acp.kas_subagent_targets import _spec_pre_tool_matchers

        if not agent_id or not isinstance(cwd, str) or not cwd:
            return False
        if _consented(agent_id, cwd).get("hooks"):
            return True
        # The rule a person-kind batch leaves a target out by
        # (``kas_subagent_targets._project_target``).
        return any(_spec_pre_tool_matchers(name, _consented(name, target_dir)) for name in targets)

    try:
        if not await asyncio.to_thread(_folder_hooks):
            return False
    except Exception:  # noqa: BLE001 - an unreadable consented spec fails closed
        logger.info(
            "folder hooks for %r or its sub-agent targets could not be read; the batch is "
            "re-projected so the turn fails closed",
            agent_id,
        )
    if await pending():
        logger.info(
            "%s: sub-agent work runs on the session; a person's turn runs without the "
            "folder agent's hooks",
            key,
        )
        return False
    return True
