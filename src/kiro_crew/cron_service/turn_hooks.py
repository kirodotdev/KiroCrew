"""A cron turn's agent spec hooks, on a backend whose harness never runs them.

kiro-cli runs an agent spec's ``hooks`` on every session. On KAS Crew fires them
in its place: a dashboard chat, a sub-agent, the task runner and a channel turn
already do, and a cron turn does through :class:`CronTurnHooks`, which is the
channel seam (:class:`kiro_crew.messaging.tool_hooks.ChannelTurnHooks`) for a turn
nobody watches:

* ``AgentSpawn`` on a new session, then ``UserPromptSubmit`` with the job's own
  prompt, into the prompt's ``[Hook context]`` block;
* the spec's ``PreToolUse`` on each permission request, first, on a turn whose
  backend never receives the spec (``TurnSpecHooks.gated``) and whose spec has
  one, with no Hooks-page hook beside it;
* ``Stop`` with the reply once the turn landed.

The Hooks page's hooks keep the role they have on a cron turn: none of them
runs here, and their PreToolUse stays the informational fire on each tool call. A cron claim re-projects a stale KAS
batch (:func:`kiro_crew.agent_sdk.spec_hooks.reproject_unattended_claim`). When
the session is still stale for the agent it runs (a PreToolUse hook added since
covers a call it auto-approves: two resets did not cure it, or a mid-turn switch
moved to such a batch), the turn runs none of the spec's hooks, as before, and
its result says so; the next claim re-projects it.
"""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.messaging.tool_hooks import ChannelTurnHooks

logger = logging.getLogger("kiro_crew.cron")


class CronTurnHooks:
    """One cron turn's agent spec hooks, as ``stream_and_collect(spec_hooks=...)`` asks them."""

    def __init__(
        self, provider: Any, hooks: ChannelTurnHooks, agent: str, *, fresh: bool, stale: bool
    ) -> None:
        self._provider = provider
        self._hooks = hooks
        self.agent = agent
        self._fresh = fresh
        self.stale = stale

    @classmethod
    async def prepare(
        cls, provider: Any, agent: str, session_key: str, prompt: str, *, fresh_session: bool
    ) -> CronTurnHooks:
        """The hooks for a turn on *provider* running *agent* (``""``: the session's)."""
        # Deferred: the agent_sdk package reaches acp.client, which is still
        # initializing when the gateway loads the cron modules.
        from kiro_crew.agent_sdk.spec_hooks import (
            TurnSpecHooks,
            hook_projection_stale,
            running_agent,
        )
        from kiro_crew.messaging.script_hooks import ScriptHookTurn
        from kiro_crew.messaging.tool_hooks import ChannelTurnHooks

        agent = running_agent(provider, agent)
        turn = ScriptHookTurn(session_key, prompt, store_hooks=False)
        # Cron is unchanged by a folder's project-hooks choice: its turn never
        # reads a KAS folder agent's own hooks, only the user-level spec's.
        hooks = await ChannelTurnHooks.prepare(provider, agent, turn, project_hooks=False)
        stale = await cls._stale(
            provider,
            agent,
            hooks.spec,
            functools.partial(hook_projection_stale, project_hooks=False),
        )
        if stale:
            empty = TurnSpecHooks([], hooks.spec.cwd, False, False, hooks.spec.lost)
            hooks = ChannelTurnHooks(provider, turn, empty)
        return cls(provider, hooks, agent, fresh=fresh_session, stale=stale)

    @staticmethod
    async def _stale(provider: Any, agent: str, spec: Any, stale_check: Any) -> bool:
        if not spec.gated or spec.unreadable or not spec.hooks:
            return False
        if not await stale_check(provider, agent):
            return False
        logger.warning(
            "agent %r: its session's batch predates its PreToolUse hooks; "
            "no spec hooks run on this cron turn",
            agent,
        )
        return True

    @property
    def gated(self) -> bool:
        """Whether the spec's PreToolUse hooks gate this turn's permission requests."""
        from kiro_crew.hooks import HOOK_EVENT_PRE_TOOL_USE

        spec = self._hooks.spec
        return spec.gated and (
            spec.unreadable or any(h.event == HOOK_EVENT_PRE_TOOL_USE for h in spec.hooks)
        )

    async def refusal(self, event: Any) -> str | None:
        """Why the spec's PreToolUse hooks refuse *event*'s permission request, or ``None``.

        The spec's hooks alone, through an empty store, so a Hooks-page hook keeps
        the informational role it has on a cron turn. Governed under the turn's own
        session key.
        """
        from kiro_crew.hooks import ScriptHookStore, permission_pre_tool_block

        spec = self._hooks.spec
        if spec.unreadable:
            # deferred: tool_permission imports llm_helpers, which reaches the cron path.
            from kiro_crew.tool_permission import SPEC_HOOKS_UNREADABLE

            return SPEC_HOOKS_UNREADABLE
        sid = getattr(self._provider, "session_id", "")
        return await permission_pre_tool_block(
            ScriptHookStore(load=False),
            spec.hooks,
            spec.cwd,
            str(getattr(event, "title", "") or ""),
            getattr(event, "tool_input", None),
            tool_identity=str(getattr(event, "tool_name", "") or ""),
            mcp_server=str(getattr(event, "mcp_server_name", "") or ""),
            harness_tool_id=str(getattr(event, "harness_tool_id", "") or ""),
            session_key=self._hooks.turn.session_key,
            acp_session_id=sid if isinstance(sid, str) and sid else None,
        )

    async def observe(self, event: Any) -> None:
        """Re-read the spec after a mid-turn agent switch, with the same staleness rule."""
        from kiro_crew.agent_sdk import CONTEXT_EVENT_AGENT_CHANGED

        if getattr(event, "kind", "") != CONTEXT_EVENT_AGENT_CHANGED:
            return
        from kiro_crew.agent_sdk.spec_hooks import session_agent

        turn = self._hooks.turn
        agent = session_agent(self._provider, str(getattr(event, "text", "") or ""))
        switched = await CronTurnHooks.prepare(
            self._provider, agent, turn.session_key, turn.prompt, fresh_session=False
        )
        self._hooks, self.agent, self.stale = switched._hooks, switched.agent, switched.stale

    async def prompt_context(self) -> list[str]:
        return await self._hooks.prompt_context(is_new=self._fresh)

    async def stop(self, reply: str) -> None:
        """Fire ``Stop`` with *reply*, redacted as the dashboard's Stop hook gets it."""
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls

        await self._hooks.stop(redact_credentials(redact_exfiltration_urls(reply)[0])[0])

    def notices(self) -> str:
        """The notices this turn's result leads with, or ``""``."""
        from kiro_crew.agent_sdk.spec_hooks import spec_keys_notice

        lines = []
        if self.stale:
            lines.append(
                f"ℹ️ The session of agent {self.agent} started before its PreToolUse "
                "hooks changed, so none of the agent's hooks ran on this run. A new "
                "session runs them."
            )
        lost = self._hooks.spec.lost
        if lost:
            lines.append(spec_keys_notice(self.agent, list(lost)))
        return "\n\n".join(lines)
