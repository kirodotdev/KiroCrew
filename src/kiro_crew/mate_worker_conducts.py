"""What a Bot's conducting worker receives on top of its worker spec.

A worker the Bot's own thread opened, while the Bot's owner-set ``worker_conducts``
switch is on, runs as a conductor for its item (``members.conducting_worker_session``;
``rfc-bot-workers-conduct.md``). Its session-control mount rides the session array
the way the thread's does; this module adds the rest to the worker's PROJECTED spec,
on the governed merge ``mate_worker.resolve_worker_spec`` builds and before the Bot's
own revocations and held approvals strip it as they strip every grant:

* the conductor's work-ledger grants (``agent._LEDGER_CONDUCTOR_WORK_GRANTS``) and
  the conductor-mode dashboard verbs (``agent._CONDUCTOR_DASHBOARD_GRANTS``), back
  through the governance ceiling so a withheld verb stays mounted and prompts. The
  dashboard verbs ride the spec, not the KAS projection, so the Bot's revocations and
  held limits strip them as they strip every grant; the session-control mount is
  what names ``@kirocrew-dashboard`` in the KAS agent's ``tools``
  (``kas_agents.to_client_custom_agent``);
* one prompt piece, ``worker.conducts``, rendered from the shared conductor rules
  (``agent._LEDGER_CONDUCTOR_RULES``) exactly as the Bot's ``conductor.patrol`` is, so
  a rule every conductor carries is written once (``test_mate_conductor_parity.py``);
* the ``goal-conductor`` skill, mapped only when the spec already maps skills: an
  unmapped ``kirocrew-mate-worker`` is a whole-catalog template that reaches the
  skill already, and a mapping would narrow it to that one skill.

With the switch off nothing here runs, and the worker's spec is byte for byte what
it is today.

The switch also bounds the Bot's workers: while it is on and the owner set no
``max_workers`` (0, no limit), the Bot's effective ``Workers at once`` is
``BOT_MAX_WORKERS_CONDUCTING`` (:func:`effective_worker_cap_refusal`, the one cap
check ``session_create``, ``session_revive`` and ``session_fork`` ask). A limit the
owner set is used as is, and with the switch off the check is today's.

What a running worker does per call (its children forced onto the Bot's worker, the
own-child exemption, the bound for its own creates) follows the kit its session
started with, read by :func:`holds_conducting_kit`, never the live switch: a switch
change reaches it at its next start, which the owner's save brings by reloading the
Bot's workers (``agent_admin.crew_update``, ``crewmate_worker_reload``). The spec's
kit follows the same mount wherever a start is composed (``mate_worker.worker_session_spec``'s
*mounted*, decided by :func:`start_mounts_session_control` before the spec is read), so
a start that mounts nothing, a re-inject of a worker that started plain included,
projects none of this module's kit.

A fork the Bot's thread makes of its worker while the switch is on conducts too
(:func:`thread_fork_conducts`), so ``session_fork`` seeds it from the Bot's Profile
like a created worker instead of copying the thread's trust.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew.config.sections_session import BOT_MAX_WORKERS_CONDUCTING

logger = logging.getLogger(__name__)

#: The crewmate prompt piece a conducting worker is given (``crewmate_prompts.json``).
CONDUCTS_PIECE = "worker.conducts"


def conducting_worker_kit(spec: dict[str, Any]) -> dict[str, Any]:
    """*spec*, a worker's governed merge, with the conducting kit layered on, in place.

    The caller has already decided the session conducts; this function reads no
    registry and no config. Returns *spec*.
    """
    # Call-time imports, as ``mate_worker.mate_worker_prompt`` makes them: the
    # ``agent`` facade's module graph is heavy and must not load with every
    # importer of the worker projection, and the rules live behind that facade.
    from kiro_crew import agent as agent_mod
    from kiro_crew.context_assembly.member import ledger_rules_block
    from kiro_crew.platform.governance import sanitize_agent_config_governance

    allowed = [ref for ref in spec.get("allowedTools") or [] if isinstance(ref, str)]
    for ref in (*agent_mod._LEDGER_CONDUCTOR_WORK_GRANTS, *agent_mod._CONDUCTOR_DASHBOARD_GRANTS):
        if ref not in allowed:
            allowed.append(ref)
    spec["allowedTools"] = allowed
    piece = ledger_rules_block(CONDUCTS_PIECE)
    own = spec.get("prompt") if isinstance(spec.get("prompt"), str) else ""
    spec["prompt"] = f"{own.rstrip()}\n\n{piece}" if own else piece
    # Every entry stays: a knowledge base is an object, not a string
    # (``agent_capabilities_resources``), and only a string entry maps a skill.
    raw = spec.get("resources")
    resources = list(raw) if isinstance(raw, list) else []
    if any(isinstance(entry, str) and entry.startswith("skill://") for entry in resources):
        for uri in agent_mod.prompt_skill_uris("goal-conductor"):
            if uri not in resources:
                resources.append(uri)
        spec["resources"] = resources
    # The grants cross the same ceiling every other grant crosses.
    sanitize_agent_config_governance(spec)
    return spec


def holds_conducting_kit(state: Any, slot: Any) -> bool:
    """Whether the session on *slot* runs with the conducting kit its start gave it.

    True when the slot is thread-born (``members.is_thread_born_worker_session``),
    known as its Bot's worker (``members.is_crewmate_worker_session``), and its
    running handle took the session-control mount at start: its live provider's
    ``member_dispatch_mounted`` is True, which only the composer that mounted the
    server for that start sets. Never the live switch, so the parts read per call
    (the hand-off onto the Bot's worker, the own-child exemption, the bound of 4)
    keep a conducting worker as it started until its next start, which a switch
    change brings (``crewmate_worker_reload``), and keep a plain worker plain. No
    live provider, or one with no recorded answer, holds no kit. Dict and
    attribute reads only, so it is safe on the loop.
    """
    from kiro_crew import members
    from kiro_crew.dashboard.chat_utils import effective_session_key

    if not members.is_thread_born_crewmate_worker(str(getattr(slot, "key", "") or "")):
        return False
    get_provider = getattr(getattr(state, "sessions", None), "get_provider", None)
    provider = get_provider(effective_session_key(slot)) if get_provider is not None else None
    return getattr(provider, "member_dispatch_mounted", None) is True


def start_mounts_session_control(
    session_key: str | None,
    backend: str,
    agent: str | None,
    work_dir: Any = None,
    config: Any = None,
) -> bool:
    """Whether a session start on *backend* for *session_key* mounts session control.

    The read the runtime's create and load paths make for that start, as one
    predicate: a backend that can carry the mount (``ACP_BACKENDS_MEMBER_DISPATCH``),
    a key that takes it (``members.mounts_session_control``: a member DM, or a worker
    the Bot's thread opened while the Bot's switch is on, read from *config* when
    given), and ``kirocrew-dashboard`` not switched off for *agent*
    (``session_mcp.session_mcp_server_is_disabled``, scoped to *work_dir* as those
    paths scope it, and for a worker read from the spec built for *session_key*, its
    Bot's file included, as its composition reads it). A worker's conducting kit
    follows this answer wherever its start is composed before the mount
    (``mate_worker.worker_session_spec``'s *mounted*), and the per-chat client mounts
    on it. Blocking (the switch and the spec reads): callers on the event loop run it
    off it.
    """
    from kiro_crew import members
    from kiro_crew.acp_backends import ACP_BACKENDS_MEMBER_DISPATCH
    from kiro_crew.agent_sdk.agent_specs import session_mcp_server_is_disabled
    from kiro_crew.agent_sdk.backends import overlay_project_scope

    if backend not in ACP_BACKENDS_MEMBER_DISPATCH:
        return False
    if not members.mounts_session_control(session_key, config):
        return False
    # The checkout the runtime's check reads the spec from (``_disable_check_scope``),
    # from the one decider it reads too.
    scope = overlay_project_scope(backend, work_dir).get("work_dir")
    return not session_mcp_server_is_disabled(
        members.MEMBER_DISPATCH_SERVER, agent, work_dir=scope, session_key=session_key or ""
    )


def effective_worker_cap_refusal(
    state: Any, execution: Any, caller_conducts: bool = False
) -> str | None:
    """Why a slot running *execution* may not open under its Bot's effective limit.

    ``None`` when it may. The check ``session_create``, ``session_revive`` and
    ``session_fork`` ask in the synchronous window that mints the slot. When the
    owner set no ``max_workers`` and either the Bot's ``worker_conducts`` switch is
    on or the caller holds the conducting kit (*caller_conducts*,
    :func:`holds_conducting_kit`: a conducting worker keeps the bound it started
    under until its next start), the existing
    ``bot_worker_cap.bot_worker_cap_refusal`` is asked under
    ``BOT_MAX_WORKERS_CONDUCTING``. Every other case, a limit the owner set or a
    Bot whose switch is off and whose workers have restarted, IS
    ``bot_worker_cap.worker_cap_refusal``, so what that check counts applies
    unchanged. Nothing in ``bot_worker_cap`` is edited.
    """
    from kiro_crew.dashboard.state import (
        bot_max_workers,
        bot_worker_cap_refusal,
        own_worker_member,
        worker_cap_refusal,
    )

    member_id = own_worker_member(execution)
    if (
        member_id
        and (caller_conducts or _bot_conducts(member_id))
        and not bot_max_workers(member_id)
    ):
        return bot_worker_cap_refusal(state, member_id, BOT_MAX_WORKERS_CONDUCTING)
    return worker_cap_refusal(state, execution)


def thread_fork_conducts(caller_key: str, execution: Any) -> bool:
    """Whether a fork *caller_key* makes of a session running *execution* conducts.

    True for the Bot's own thread (a member DM key) forking one of the Bot's own
    workers while the Bot's ``worker_conducts`` switch is on, read as the fork's
    bound reads it (:func:`effective_worker_cap_refusal`). Such a fork is a worker
    of the conducting tree, so it takes the Bot's Profile posture once its record is
    bound, as a created worker does, rather than a copy of the thread's trust that
    the thread's later mode change would not reach (``approval_inheritance`` skips a
    Bot's workers).
    """
    from kiro_crew import members
    from kiro_crew.dashboard.state import own_worker_member

    member_id = own_worker_member(execution)
    return (
        bool(member_id) and members.is_member_session_key(caller_key) and _bot_conducts(member_id)
    )


def _bot_conducts(member_id: str) -> bool:
    """Whether Bot *member_id*'s owner has its ``worker_conducts`` switch on now.

    Read as ``bot_worker_cap.bot_max_workers`` reads the limit (the live snapshot,
    else the cached load), so the switch and the limit come from one config. An
    unreadable record reads as off, which leaves today's check, and that check
    logs the unreadable config itself.
    """
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.crewmate_harness import crew_for_member_id

    try:
        snap = live.snapshot()
        cfg = snap if snap is not None else KiroCrewConfig.load()
        record = cfg.agents.get(crew_for_member_id(cfg, member_id))
    except Exception:  # noqa: BLE001 - an unreadable switch is off: today's check applies
        logger.debug("could not read worker_conducts for %s", member_id, exc_info=True)
        return False
    return getattr(record, "worker_conducts", False) is True
