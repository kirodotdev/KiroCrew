"""Spawn SDK — app-scoped background agent spawning.

An app doing unattended work (a poller, a watcher, a planner) needs to start an
agent turn with no user present. The host already owns that machinery
(``SubagentManager``: concurrency accounting, a reaper for wedged agents, SEL
audit), so this is a permissioned adapter onto it, not a second spawner.

It arrives the way ``cron`` / ``events`` / ``storage`` do — declared as
``permissions.spawn``, injected by the gateway, ``None`` otherwise — so "which
apps may start an agent" reads off the manifest rather than off an import graph.
The core never imports an app to hand it this.

``run`` RAISES on refusal rather than returning a falsy id: the host can decline
(approval policy, caps), and a caller that reads a decline as success waits on a
spawn that never happened instead of counting a failure and backing off.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from kiro_crew.agent_discovery import list_agents
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)


def _audit_spawn_denied(app: str, agent: str, reason: str) -> None:
    """Emit a SEL denial record for an SDK-side spawn refusal.

    The SDK rejects an empty-agent, unverifiable, or cross-app spawn BEFORE it
    reaches ``SubagentManager`` (whose own governance gate emits the denial
    record for spawns that get that far). Without this, an authorization refusal
    at the SDK boundary would leave no security-event trail. Best-effort: an
    audit failure must never turn a denial into a crash.
    """
    try:
        sel().log_tool_invocation(
            session_key="",
            source="app_spawn_sdk",
            tool_name="spawn_run",
            outcome="denied",
            error=reason,
            metadata={"app": app, "agent": agent or "default"},
        )
    except Exception:  # noqa: BLE001 — auditing must never break the refusal
        logger.warning("Failed to SEL-audit denied spawn for app %r", app)


#: What the gateway injects: ``(task, agent, silent, model, app) -> awaitable[spawn id]``.
#: ``app`` is the calling app's name — threaded so the spawn's governance check
#: can resolve that app's OWN profile (blast-radius containment), not just the
#: policy ceiling. An empty id means the host declined.
SpawnImpl = Callable[[str, str, bool, str, str], Awaitable[str]]


class SpawnError(RuntimeError):
    """The host declined or could not start the requested agent."""


#: Optional probe the gateway injects alongside the impl:
#: ``(spawn_id) -> bool`` — True once that agent has finished (any outcome).
DoneProbe = Callable[[str], bool]


class SpawnSDK:
    """App-scoped view of the host's background-agent spawning."""

    def __init__(self, app_name: str, impl: SpawnImpl, done_probe: DoneProbe | None = None) -> None:
        self._app_name = app_name
        self._impl = impl
        self._done_probe = done_probe

    def is_done(self, spawn_id: str) -> bool:
        """True once the spawned agent has finished (success OR failure).

        An unattended caller that serializes its spawns (a poller, a planner)
        needs a completion signal to release its lock early instead of always
        sitting out the full timeout. Unknown ids read as done so a caller can
        never wedge on an id the host has already forgotten. Returns False when
        the host injected no probe — the caller's timeout then stands alone.
        """
        if self._done_probe is None:
            return False
        try:
            return bool(self._done_probe(spawn_id))
        except Exception:  # noqa: BLE001 — a probe failure must not break the caller
            return False

    async def run(
        self, task: str, agent: str = "", *, silent: bool = False, model: str = ""
    ) -> str:
        """Start a background agent for *task*; return its spawn id.

        ``agent`` is REQUIRED and must name the app's own restricted background
        agent. An unattended spawn is bounded by that agent's tool surface, not by
        an approval prompt (there is no user to ask, so ``approval_mode="auto"``),
        and ``capabilities.spawn.scopes.agents`` is enforced against the NAMED
        agent. An empty name would fall through to the host's DEFAULT agent — full
        tool surface, auto-approved — AND skip the agent-scope evaluation, so a
        profile that restricts spawning to specific agents would be silently
        bypassed. The parameter keeps its ``""`` default only so the failure is a
        loud raise here rather than a TypeError. ``model`` overrides the agent's
        default model for this run — unattended work is where users most want a
        cheaper model, since nobody is present to weigh cost against quality.
        """
        if not task.strip():
            raise SpawnError("spawn task is empty")
        if not agent.strip():
            reason = (
                f"spawn for app {self._app_name!r} requires a named agent: an empty "
                "agent would run the host default with full, auto-approved tools and "
                "skip the spawn agent-scope policy"
            )
            _audit_spawn_denied(self._app_name, agent, reason)
            raise SpawnError(reason)
        try:
            spawn_id = await self._impl(task, agent, silent, model, self._app_name)
        except SpawnError:
            raise
        except Exception as exc:  # noqa: BLE001 — normalise to one failure type
            raise SpawnError(f"spawn failed for app {self._app_name!r}: {exc}") from exc
        if not spawn_id:
            raise SpawnError(
                f"host declined the spawn for app {self._app_name!r} "
                f"(agent={agent or 'default'!r})"
            )
        logger.info(
            "App %s spawned background agent %s (id=%s, silent=%s)",
            self._app_name,
            agent or "default",
            spawn_id,
            silent,
        )
        return spawn_id


def build_spawn_impl(subagents: object) -> SpawnImpl:
    """Adapt a live ``SubagentManager`` to :data:`SpawnImpl`.

    ``approval_mode="auto"`` is REQUIRED, not incidental: the host's approval
    chain ends in "rejected" for a caller with no user to ask, so every
    unattended spawn would be declined. What bounds such a spawn is the agent's
    own tool surface, which is why an app passes its restricted background agent.

    Returns "" when the manager declines, which :meth:`SpawnSDK.run` turns into a
    raise; a refused spawn that comes back as a DONE record carrying ``error``
    raises here, because an id alone does not mean it is running.
    """

    async def _impl(task: str, agent: str, silent: bool, model: str, app: str) -> str:
        if subagents is None:
            raise SpawnError("no subagent manager on this gateway")
        # The named agent must EXIST. SubagentManager validation silently replaces
        # an unknown name with "" and runs the host DEFAULT agent — full tool
        # surface, approval_mode="auto" — so a typo in an app's agent name would
        # ESCALATE from the app's restricted background agent to unrestricted
        # auto-approved execution. Fail closed: refuse a name we cannot confirm is
        # a real agent rather than fall back.
        try:
            # Off the loop: list_agents() scans the agents dir and JSON-parses each
            # file. On a cold or large directory that read would block the gateway
            # loop — stalling chat and the heartbeat — so it runs in a thread.
            agents = await asyncio.to_thread(list_agents)
            # Restrict to THIS app's OWN agents. Materialized app agents are named
            # `<app>--<agent>.json`, so the filename prefix is the ownership proof.
            # Without it an app could name the GLOBAL host agent ("kirocrew") or
            # another app's agent — which the manager would then run with
            # approval_mode="auto", a full-privilege escalation from the app's own
            # restricted background agent.
            prefix = f"{app}--"
            known = {a.name for a in agents if a.filename.startswith(prefix)}
        except Exception as exc:  # noqa: BLE001 — cannot confirm → refuse
            reason = f"cannot verify agent {agent!r} for app {app!r}: {exc}"
            _audit_spawn_denied(app, agent, reason)
            raise SpawnError(reason) from exc
        if agent not in known:
            reason = (
                f"app {app!r} may only spawn its OWN agents ({prefix}*); {agent!r} is not one "
                "(refusing to run the host default or another app's agent)"
            )
            _audit_spawn_denied(app, agent, reason)
            raise SpawnError(reason)
        # ``app`` is forwarded so SubagentManager.spawn can resolve the calling
        # app's per-app governance profile — a profile that denies
        # ``capabilities.spawn`` for this app must win even when the policy
        # ceiling alone would permit. Omitting it was a Level-2 (PROFILE) bypass.
        info = subagents.spawn(  # type: ignore[attr-defined]
            task,
            agent=agent,
            silent=silent,
            approval_mode="auto",
            model=model or None,
            app=app,
            # Already confirmed to exist via the off-loop list_agents() above;
            # skip the manager's synchronous re-scan on the event loop.
            _agent_prevalidated=True,
        )
        if info is None:
            return ""
        if getattr(info, "error", ""):
            raise SpawnError(str(info.error))
        return str(getattr(info, "id", "") or "")

    # Ride the probe on the impl callable rather than adding a second parameter
    # to every layer that threads spawn_impl (gateway -> lifecycle -> context).
    # build_app_context reads it back via getattr.
    _impl.done_probe = build_done_probe(subagents)  # type: ignore[attr-defined]
    return _impl


def build_done_probe(subagents: object) -> DoneProbe:
    """Adapt a live ``SubagentManager`` to :data:`DoneProbe`.

    An id the manager no longer tracks reads as done: the reaper prunes
    records, and "gone" must never hold a caller's serial lock open.
    """

    def _probe(spawn_id: str) -> bool:
        if subagents is None or not spawn_id:
            return True
        info = subagents.get(spawn_id)  # type: ignore[attr-defined]
        return info is None or bool(getattr(info, "done", False))

    return _probe
