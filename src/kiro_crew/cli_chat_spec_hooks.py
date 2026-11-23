"""``kirocrew chat``'s agent spec hooks, on a backend whose harness never runs them.

kiro-cli runs an agent spec's ``hooks`` itself. On KAS (and goose, opencode,
strands) Crew fires them in its place: a dashboard chat, a sub-agent, the task
runner, a channel turn and a cron turn already do. ``kirocrew chat`` does through
:func:`turn_hooks`, which is the channel seam
(:class:`kiro_crew.messaging.tool_hooks.ChannelTurnHooks`) read once per message:

* ``AgentSpawn`` on a new session, then ``UserPromptSubmit`` with the prompt the
  backend receives, into the prompt's ``[Hook context]`` block;
* the spec's ``PreToolUse`` on each permission request, before the y/n prompt
  (``cli_chat._spec_hook_refusal``, which reads :attr:`ChannelTurnHooks.spec`);
* ``PostToolUse`` on each call's terminal result;
* ``Stop`` with the reply once the turn landed.

The Hooks page's hooks, and the user's standalone ``~/.kiro/hooks`` files on the
events that cannot refuse a call, stay out of the CLI, as on a cron turn
(``store_hooks=False``). A new session also prints the notices a dashboard posts
for the spec: keys this backend never receives, and a spec that cannot be read.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

from kiro_crew.terminal_safe import safe_terminal_line

if TYPE_CHECKING:
    from kiro_crew.messaging.tool_hooks import ChannelTurnHooks


async def turn_hooks(
    provider: Any, session_key: str, kiro_agent: str, prompt: str
) -> ChannelTurnHooks | None:
    """The spec hooks one CLI message fires, or ``None`` where Crew fires none.

    *kiro_agent* is the agent the CLI started (``""``: the session's); the agent
    the live session runs wins over it (``spec_hooks.running_agent``).
    """
    # Deferred, as in messaging.tool_hooks: the agent_sdk package reaches acp.client.
    from kiro_crew.agent_sdk.capabilities import capabilities_of
    from kiro_crew.messaging.script_hooks import ScriptHookTurn
    from kiro_crew.messaging.tool_hooks import ChannelTurnHooks

    if not capabilities_of(provider).crew_fires_spec_hooks:
        return None
    return await ChannelTurnHooks.prepare(
        provider, kiro_agent, ScriptHookTurn(session_key, prompt, store_hooks=False)
    )


async def start(
    provider: Any, session_key: str, kiro_agent: str, prompt: str, *, new_session: bool
) -> tuple[ChannelTurnHooks | None, str]:
    """Open one CLI message's hooks: ``(hooks, the prompt to send)``.

    On a new session prints the spec's notices and fires ``AgentSpawn``; always
    fires ``UserPromptSubmit``. Their output goes in front of *prompt*.
    """
    from kiro_crew.messaging.script_hooks import with_hook_context

    hooks = await turn_hooks(provider, session_key, kiro_agent, prompt)
    if hooks is None:
        return None, prompt
    if new_session:
        print_session_notices(provider, hooks, kiro_agent)
    injected = await hooks.prompt_context(is_new=new_session)
    return hooks, await with_hook_context(prompt, injected)


def print_session_notices(provider: Any, hooks: ChannelTurnHooks, kiro_agent: str) -> None:
    """Print, to stderr, what a new session's spec hooks cannot do here."""
    from kiro_crew.agent_sdk.spec_hooks import running_agent, spec_keys_notice

    agent = running_agent(provider, kiro_agent) or "(unknown)"
    spec = hooks.spec
    lines = []
    if spec.unreadable:
        lines.append(
            f"ℹ The hooks of agent {agent} could not be read, so every tool call "
            "that asks for approval is refused in this session. The log names the reason."
        )
    if spec.lost:
        lines.append(spec_keys_notice(agent, list(spec.lost)))
    for line in lines:
        print(safe_terminal_line(line), file=sys.stderr)


async def stop(hooks: ChannelTurnHooks, reply: str) -> None:
    """Fire ``Stop`` with *reply*, redacted as the dashboard's Stop hook gets it."""
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    await hooks.stop(redact_credentials(redact_exfiltration_urls(reply)[0])[0])
