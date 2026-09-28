# Subagents

Source: https://kiro.dev/docs/custom-agents/subagents/ (fetched 2026-09-06)

Upstream retired `/docs/cli/chat/subagents/` and moved the page under custom
agents. This describes **upstream's** subagent model; Kiro Crew's own
`spawn_run` / `spawn_continue` contract is owned by
[subagent](../../../system-specs/modules/subagent.md).

Any custom agent can be invoked as a subagent. The main agent picks one from the
`description` field, or you name it in the prompt.

## What a subagent inherits

Shared with the main agent: steering files, MCP servers, workspace file access,
permissions configuration. Isolated per subagent: conversation history, context
window, spec state, hook triggers.

## Built-in subagents

Two internal ones need no configuration: context gathering, which explores the
project and collects relevant files, and general purpose, which handles
parallelised tasks under the default agent configuration.

## Task graphs and review loops

Subagents run in parallel, and dependent work is planned as a DAG upfront: the
graph is fixed before the first subagent starts and cannot change during
execution. A stage can also loop back to an earlier one, configured by `target`
(the stage to re-run), `trigger` (text in the stage output that fires the loop,
at least four characters) and `max_iterations` (1 to 10). A stage cannot loop to
itself and mutual loops are rejected.

## Tool availability

The default subagent has the same built-in tools as the main agent: `read`,
`write`, `shell`, `web_search`, `web_fetch`, plus configured MCP tools.
Delegating to a custom agent instead uses that agent's own `tools` and
`permissions`, so anything unlisted there is unavailable. An orchestrator agent
needs `subagent` in its own `tools` array (or `@builtin`) or it cannot delegate;
to restrict what a subagent may use, configure `tools` in the subagent's config
rather than the parent's.

## Configuring subagent access

```json
{
  "toolsSettings": {
    "subagent": {
      "availableAgents": ["reviewer", "tester", "docs-*"],
      "trustedAgents": ["reviewer", "tester"]
    }
  }
}
```

`availableAgents` is a glob list of agents this agent may spawn, and omitting it
allows all. `trustedAgents` run without permission prompts.

## CLI runtime behaviour

`Ctrl+G` opens the execution monitor for live per-subagent activity without
interrupting the main chat; `Ctrl+D` and `Ctrl+U` move between subagents and `q`
returns. `fs_read` inside the current working directory is auto-approved and
reads outside it still prompt. A non-interactive subagent cannot prompt, so it
fails fast when a tool needs approval unless the agent is in `trustedAgents`.
Each persisted subagent session records the spawning session's ID.
