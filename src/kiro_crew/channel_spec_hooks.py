"""An agent-channel member's gate on its agent spec's own PreToolUse hooks.

On a backend whose harness never receives the spec's ``hooks`` (KAS, goose,
opencode, strands), Crew fires them itself, and the projection sends every call a
``preToolUse`` hook covers to Crew as a permission request so the hook can refuse
it first. :func:`kiro_crew.channel._stream_task` answers a member's permission
requests, so it reads the spec once per turn (:func:`turn_spec`) and asks
:func:`pre_tool_refusal` before any approval tier. ``kirocrew chat`` asks it on
each permission request too, before its y/n prompt (``cli_chat._spec_hook_refusal``).

Only the spec's hooks gate here, through an empty store, so the Hooks page's hooks
stay out of an agent channel and the CLI. A spec hook blocks on exit 2 or a policy refusal to
run it, as on kiro-cli, and a spec that could not be read refuses every request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kiro_crew.hooks import ScriptHookStore, permission_pre_tool_block

if TYPE_CHECKING:
    from kiro_crew.agent_sdk.spec_hooks import TurnSpecHooks


async def turn_spec(client: Any, agent_name: str) -> TurnSpecHooks:
    """The spec hooks a member's turn on *client* gates on.

    The agent the live session runs wins over the member's configured one, as on a
    channel bot's turn (:func:`kiro_crew.agent_sdk.spec_hooks.running_agent`). A
    member that names none runs the agent its session started with.
    """
    # Deferred, as in messaging.tool_hooks: the agent_sdk package reaches acp.client.
    from kiro_crew.agent_sdk import spec_hooks
    from kiro_crew.dashboard.handlers.usage import read_effective_agent

    named = agent_name or read_effective_agent(client)
    return await spec_hooks.turn_spec_hooks(client, spec_hooks.running_agent(client, named))


async def pre_tool_refusal(
    spec: TurnSpecHooks, client: Any, event: Any, session_key: str
) -> str | None:
    """Why *spec*'s PreToolUse hooks refuse *event*'s permission request, or ``None``."""
    if not spec.gated:
        return None
    if spec.unreadable:
        # Deferred, as in messaging.tool_hooks: tool_permission imports llm_helpers.
        from kiro_crew.tool_permission import SPEC_HOOKS_UNREADABLE

        return SPEC_HOOKS_UNREADABLE
    if not spec.hooks:
        return None
    sid = getattr(client, "session_id", "")
    return await permission_pre_tool_block(
        ScriptHookStore(load=False),
        spec.hooks,
        spec.cwd,
        str(getattr(event, "title", "") or ""),
        getattr(event, "tool_input", None),
        tool_identity=str(getattr(event, "tool_name", "") or ""),
        mcp_server=str(getattr(event, "mcp_server_name", "") or ""),
        harness_tool_id=str(getattr(event, "harness_tool_id", "") or ""),
        session_key=session_key,
        acp_session_id=sid if isinstance(sid, str) and sid else None,
    )
