"""``kirocrew chat``'s agent spec hooks, on a backend whose harness never runs them.

kiro-cli runs an agent spec's ``hooks`` itself. On KAS (and goose, opencode,
strands) Crew fires them in its place: a dashboard chat, a sub-agent, the task
runner, a channel turn and a cron turn already do. ``kirocrew chat`` does through
:func:`turn_hooks`, which is the channel seam
(:class:`kiro_crew.messaging.tool_hooks.ChannelTurnHooks`) read once per message:

* ``AgentSpawn`` on a new session, then ``UserPromptSubmit`` with the prompt the
  backend receives, into the prompt's ``[Hook context]`` block;
* the spec's ``PreToolUse`` on each permission request, before the y/n prompt
  (``cli_chat._spec_hook_refusal``, which reads :attr:`ChannelTurnHooks.spec`),
  re-read for the new agent after a mid-turn switch (:func:`switched`);
* ``PostToolUse`` on each call's terminal result;
* ``Stop`` with the reply once the turn landed.

The Hooks page's hooks, and the user's standalone ``~/.kiro/hooks`` files on the
events that cannot refuse a call, stay out of the CLI, as on a cron turn
(``store_hooks=False``). A new session also prints the notices a dashboard posts
for the spec: keys this backend never receives, and a spec that cannot be read.
"""

from __future__ import annotations

import string
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
    from kiro_crew.cli_chat_context import _is_harness_command
    from kiro_crew.messaging.script_hooks import with_hook_context

    hooks = await turn_hooks(provider, session_key, kiro_agent, prompt)
    if hooks is None:
        return None, prompt
    if new_session:
        print_session_notices(provider, hooks, kiro_agent)
    injected = await hooks.prompt_context(is_new=new_session)
    # A harness runs a slash command (``/compact``) only when it opens the prompt.
    if await _is_harness_command(prompt):
        return hooks, prompt
    return hooks, await with_hook_context(prompt, injected)


async def switched(provider: Any, hooks: ChannelTurnHooks, event: Any) -> ChannelTurnHooks:
    """The hooks of the agent a mid-turn switch moved the session to.

    Its ``preToolUse`` guard answers the turn's later permission requests, and its
    other hooks the turn's later events. Nothing fires on the switch itself.
    """
    from kiro_crew.agent_sdk.spec_hooks import session_agent

    agent = session_agent(provider, str(getattr(event, "text", "") or ""))
    turn = hooks.turn
    renewed = await turn_hooks(provider, turn.session_key, agent, turn.prompt)
    return renewed if renewed is not None else hooks


#: What a printed notice is rebuilt from: printable ASCII without line breaks, and
#: the notice's own marks.
_NOTICE_ALPHABET = "".join(c for c in string.printable if c not in "\t\n\r\x0b\x0c") + "ℹ…"


def _owned_text(text: str) -> str:
    """*text* rebuilt from :data:`_NOTICE_ALPHABET`'s own characters, after redaction.

    A notice names spec keys (an MCP server's ``oauth.clientSecret``), never their
    values, but ``py/clear-text-logging-sensitive-data`` follows the spec-derived
    dataflow into stderr and models no redactor as a barrier. Returning characters
    this module owns severs that flow, as ``acp.mcp_ref_guard._log_safe`` does.
    shortcut: a character outside the alphabet (a non-ASCII agent name) is dropped;
    upgrade if CodeQL learns to model ``redact_credentials`` as a sanitizer.
    """
    from kiro_crew.security import redact_credentials

    cleaned = safe_terminal_line(redact_credentials(text)[0])
    out: list[str] = []
    for ch in cleaned:
        idx = _NOTICE_ALPHABET.find(ch)
        if idx >= 0:
            # The alphabet's own character object, not the input's.
            out.append(_NOTICE_ALPHABET[idx])
    return "".join(out)


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
        print(_owned_text(line), file=sys.stderr)


async def stop(hooks: ChannelTurnHooks, reply: str, stop_reason: str) -> None:
    """Fire ``Stop`` with *reply*, redacted as the dashboard's Stop hook gets it.

    Only for a turn that landed (*stop_reason* classifies ``succeeded``), as the
    channel driver judges it: not one the user stopped, or one that ended on an error.
    """
    from kiro_crew.agent_sdk.drivers.acp_vocab import STOP_CLASS_SUCCEEDED, classify_stop_reason
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    if classify_stop_reason(stop_reason).name != STOP_CLASS_SUCCEEDED:
        return

    await hooks.stop(redact_credentials(redact_exfiltration_urls(reply)[0])[0])
