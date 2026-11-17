"""A channel turn's script hooks, the agent spec's own among them.

The dashboard chat, subagent and task-runner turn loops fire an agent spec's own
``hooks`` on a backend whose harness never receives them, through
:func:`kiro_crew.agent_sdk.spec_hooks.turn_spec_hooks`. A channel turn runs through
:class:`kiro_crew.messaging.driver.TurnDriver`, so :class:`ChannelTurnHooks` is
that seam for it: read once when the turn starts, before anything streams, and
asked by the driver at each point a hook event falls on.

* ``PreToolUse`` gates each permission request
  (:func:`kiro_crew.hooks.permission_pre_tool_block`), after the security gate's
  deny and before every approval, the gate's own auto-approve included. The
  Hooks page's hooks gate on every backend and every turn that admits tools,
  because a deny guard can only refuse; the spec's own gate it wherever Crew runs
  them. A spec that could not be read refuses every request. On a turn whose backend
  runs the spec's hooks itself, the Hooks page's hooks also see each tool-call
  frame, as on the dashboard (there a granted call raises no permission request),
  where its :class:`~kiro_crew.messaging.script_hooks.ScriptHookTurn` runs them.
* ``PostToolUse`` fires once per call, on its terminal result, with the spec's
  matchers meeting the call by the id the turn learned for it
  (:func:`kiro_crew.agent_sdk.spec_hooks.post_tool_identity`).
* ``AgentSpawn``, ``UserPromptSubmit`` and ``Stop`` are
  :mod:`kiro_crew.messaging.script_hooks`, handed the same spec.

The Hooks page's hooks on the events that cannot refuse a call run only where the
turn's :class:`~kiro_crew.messaging.script_hooks.ScriptHookTurn` says they do, and
so do the user's standalone ``~/.kiro/hooks/*.json`` files KAS folds into the
spec's (:func:`kiro_crew.agent_sdk.spec_hooks.merge_user_hook_files`): those are
the operator's, not the agent's. The spec's run on every channel turn that admits
tools, as the harness that would otherwise run them does on every turn. A turn
that admits none (a guest's) reads no spec: its tool-less agent mounts no hook.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from kiro_crew.hooks import (
    HOOK_EVENT_POST_TOOL_USE,
    fire_tool_hooks,
    get_global_hook_store,
    permission_pre_tool_block,
    pre_tool_match_names,
)
from kiro_crew.messaging.script_hooks import (
    ScriptHookTurn,
    fire_agent_spawn,
    fire_prompt_submit,
    fire_stop,
    observer_store,
    spec_hook_kwargs,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:
    from kiro_crew.agent_sdk.spec_hooks import PostToolIdentity, TurnSpecHooks

logger = logging.getLogger(__name__)

#: The longest tool output a PostToolUse hook is handed, as on the dashboard.
_POST_TOOL_OUTPUT_CHARS = 2000

#: The ``spec_origin`` of a hook from the user's standalone ``~/.kiro/hooks``
#: files (:func:`kiro_crew.agent_sdk.spec_hooks.user_hook_files`).
_USER_FILE_ORIGIN = "user_file"


def _title(event: Any) -> str:
    raw = str(getattr(event, "title", "") or "")
    return raw[9:] if raw.startswith("Running: ") else raw


class ChannelTurnHooks:
    """One channel turn's script hooks: *turn* says whose, *spec* is the agent's own.

    *spec* gates the permission requests whole. The events that cannot refuse a
    call fire :attr:`observed`, which leaves out the user's standalone hook files
    on a turn the Hooks page's hooks do not run for.
    """

    def __init__(self, provider: Any, turn: ScriptHookTurn, spec: TurnSpecHooks) -> None:
        self._provider = provider
        self.turn = turn
        self.spec = spec
        self.observed = (
            spec
            if turn.store_hooks
            else spec._replace(hooks=[h for h in spec.hooks if h.spec_origin != _USER_FILE_ORIGIN])
        )
        # tool_call_id -> the title a Hooks-page PostToolUse hook matches, and the
        # names a spec one does. Turn-local, bounded by the turn's tool calls.
        self._titles: dict[str, str] = {}
        self._named: dict[str, PostToolIdentity] = {}
        # tool_call_id -> the harness-written tool name and MCP server, which a
        # Hooks-page matcher meets on every backend.
        self._identity: dict[str, tuple[str, str]] = {}

    @classmethod
    async def prepare(
        cls,
        provider: Any,
        agent: str,
        turn: ScriptHookTurn,
        *,
        admits_tools: bool = True,
        project_hooks: bool = True,
    ) -> ChannelTurnHooks:
        """The hooks for a turn on *provider* running *agent* (``""``: the session's).

        A turn that admits no tool reads no spec and fires none of its hooks.
        *project_hooks* false (a cron turn) leaves out a KAS folder agent's own hooks.
        """
        # Deferred, as in script_hooks: the agent_sdk package reaches acp.client,
        # which is still initializing when a channel module loads this one.
        from kiro_crew.agent_sdk import spec_hooks

        if not admits_tools:
            return cls(provider, turn, spec_hooks.TurnSpecHooks([], None, False, False))
        # The agent the live session runs, when it records one, wins over the one
        # the channel resolved for this message: a reused session keeps its agent
        # after a default-agent switch elsewhere, and its guards are the ones that
        # apply.
        agent = spec_hooks.running_agent(provider, agent)
        return cls(
            provider,
            turn,
            await spec_hooks.turn_spec_hooks(provider, agent, project_hooks=project_hooks),
        )

    def _acp_session_id(self) -> str:
        """The provider's ACP session id, read at fire time, or ``""``.

        The spec's hooks read it as ``session_id``, as kiro-cli hands it to them
        and as the dashboard's turn does; a guard that reads that key and is not
        given it crashes, and a crashed hook does not refuse.
        """
        sid = getattr(self._provider, "session_id", "")
        return sid if isinstance(sid, str) else ""

    async def prompt_context(self, *, is_new: bool) -> list[str]:
        """Fire ``AgentSpawn`` (on a new session) and ``UserPromptSubmit``; their context."""
        sid = self._acp_session_id()
        spawned = (
            await fire_agent_spawn(self.turn, self.observed, acp_session_id=sid) if is_new else []
        )
        return spawned + await fire_prompt_submit(self.turn, self.observed, acp_session_id=sid)

    async def stop(self, reply: str) -> None:
        await fire_stop(self.turn, reply, self.observed, acp_session_id=self._acp_session_id())

    def _name(self, event: Any, *, is_shell: bool, keep: bool) -> None:
        call_id = str(getattr(event, "tool_call_id", "") or "")
        if not call_id:
            return
        tool_name = str(getattr(event, "tool_name", "") or "")
        if tool_name:
            self._identity[call_id] = (
                tool_name,
                str(getattr(event, "mcp_server_name", "") or ""),
            )
        elif not keep:
            self._identity.pop(call_id, None)
        from kiro_crew.agent_sdk.spec_hooks import post_tool_identity  # deferred: see prepare

        named = post_tool_identity(
            self._provider,
            event,
            is_shell=is_shell,
            earlier=self._named.get(call_id) if keep else None,
        )
        if named is not None:
            self._named[call_id] = named
        elif not keep:
            self._named.pop(call_id, None)

    async def on_tool_call(self, event: Any) -> None:
        """Note the call's names, and on an ungated turn fire the informational PreToolUse.

        That one is the Hooks page's, and cannot refuse the call, so it runs only
        where the turn runs that page's hooks.
        """
        call_id = str(getattr(event, "tool_call_id", "") or "")
        if call_id:
            self._titles[call_id] = _title(event)
            self._name(event, is_shell=bool(getattr(event, "is_shell", False)), keep=False)
        if not self.spec.gated and self.turn.store_hooks:
            await fire_tool_hooks(
                get_global_hook_store(),
                str(getattr(event, "title", "") or ""),
                getattr(event, "tool_input", None),
                session_key=self.turn.session_key,
                tool_match_names=self._match_names(call_id, _title(event))[0],
            )

    def _match_names(
        self, call_id: str, title: str
    ) -> tuple[tuple[str, ...] | None, tuple[str, ...] | None]:
        """``(all, spec)`` names for a fire on *call_id*, as on the dashboard.

        *all* is for a Hooks-page matcher: every name the call is known by, from
        the harness-written tool name and, where the turn learned one, the spec
        identity. *spec* is the spec's kiro-cli names, learned only on a backend
        where Crew fires the spec's hooks. ``None`` keeps title matching.
        """
        named = self._named.get(call_id)
        if named is not None:
            return pre_tool_match_names(
                title,
                tool_identity=named.tool_identity,
                mcp_server=named.mcp_server,
                harness_tool_id=named.tool_id,
            )
        identity = self._identity.get(call_id)
        if identity is not None:
            return (
                pre_tool_match_names(title, tool_identity=identity[0], mcp_server=identity[1])[0],
                None,
            )
        return None, None

    async def pre_tool_refusal(self, event: Any) -> str | None:
        """Why the PreToolUse hooks refuse *event*'s permission request, or ``None``."""
        # The id the request names the tool by is what its PostToolUse matches on.
        self._name(event, is_shell=False, keep=True)
        if self.spec.unreadable:
            # deferred: tool_permission imports llm_helpers, which reaches messaging.
            from kiro_crew.tool_permission import SPEC_HOOKS_UNREADABLE

            return SPEC_HOOKS_UNREADABLE
        sid = self._acp_session_id()
        return await permission_pre_tool_block(
            get_global_hook_store(),
            self.spec.hooks,
            self.spec.cwd,
            str(getattr(event, "title", "") or ""),
            getattr(event, "tool_input", None),
            tool_identity=str(getattr(event, "tool_name", "") or ""),
            mcp_server=str(getattr(event, "mcp_server_name", "") or ""),
            harness_tool_id=str(getattr(event, "harness_tool_id", "") or ""),
            session_key=self.turn.session_key,
            acp_session_id=sid if self.spec.hooks and sid else None,
        )

    async def on_tool_result(self, event: Any) -> None:
        """Fire ``PostToolUse`` once per call, on its terminal result."""
        # deferred: see prepare
        from kiro_crew.agent_sdk.drivers.acp_vocab import TERMINAL_TOOL_STATUSES

        terminal = bool(getattr(event, "tool_final", False)) or (
            getattr(event, "tool_status", None) in TERMINAL_TOOL_STATUSES
        )
        call_id = str(getattr(event, "tool_call_id", "") or "")
        if not terminal or not call_id:
            return
        title = self._titles.pop(call_id, "")
        match_names, spec_names = self._match_names(call_id, title)
        self._named.pop(call_id, None)
        self._identity.pop(call_id, None)
        store = observer_store(self.turn, self.observed)
        if store is None:
            return
        output, _ = redact_exfiltration_urls(str(getattr(event, "tool_output", "") or ""))
        output, _ = redact_credentials(output)
        try:
            await store.fire(
                HOOK_EVENT_POST_TOOL_USE,
                tool_name=title,
                tool_response={"output": output[:_POST_TOOL_OUTPUT_CHARS]},
                session_key=self.turn.session_key,
                tool_match_names=match_names,
                extra_hooks_tool_names=spec_names if self.observed.hooks else None,
                **spec_hook_kwargs(self.observed, self._acp_session_id()),
            )
        except Exception:
            logger.debug("PostToolUse hook error on a channel turn", exc_info=True)
