# Subagents

Source: https://kiro.dev/docs/custom-agents/subagents/ (fetched 2026-11-23)

Upstream retired `/docs/cli/chat/subagents/` and moved the page under custom
agents. This describes **upstream's** subagent model; Kiro Crew's own
`spawn_run` / `spawn_continue` contract is owned by
[subagent](../../../system-specs/modules/subagent.md).

Any custom agent can be invoked as a subagent. The main agent picks one from the
`description` field, or you name it in the prompt. A request naming an unknown
agent fails rather than falling back to the default agent.

## What a subagent inherits

Shared with the main agent: steering files, MCP servers, workspace file access,
permissions configuration. Isolated per subagent: conversation history, context
window, spec state, hook triggers.

## Built-in subagents

Internal ones need no configuration: context gathering, which explores the
project and collects relevant files; general purpose, which handles parallelised
tasks under the default agent configuration; validation, which checks a finished
change against the request (rolling out gradually); and, in CLI V3, introspect,
which answers questions about Kiro from its documentation.

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
needs `subagent` in its own `tools` array or it cannot delegate; to restrict what
a subagent may use, configure `tools` in the subagent's config rather than the
parent's.

## Configuring subagent access

In CLI 2.x, `toolsSettings.subagent` controls which agents may be spawned and
which start without a prompt:

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
allows all. `trustedAgents` start without a permission prompt.

CLI V3 ignores `toolsSettings` and `allowedTools`. To limit which agents an agent
may spawn, list `subagent/<name>` entries in its `tools` instead of `subagent`,
and leave `@builtin` and `*` out, since both include `invoke_sub_agent`, which can
start any agent. A start without a prompt is a `subagent` permission rule:

```json
{
  "tools": ["read", "write", "shell", "web", "subagent/reviewer", "subagent/tester"],
  "permissions": {
    "rules": [
      { "capability": "subagent", "match": ["reviewer", "tester"], "effect": "allow" }
    ]
  }
}
```

In CLI V3 and the IDE every subagent tool call is checked against both the parent
session's policy (user and workspace `permissions.yaml`, the parent agent's
`permissions`, earlier approvals) and the subagent's own `permissions`, and the
stricter result wins: a deny in either denies, an ask in either prompts, and a
call the parent policy has no rule for prompts even when the subagent allows it.
A subagent's own allow rules take effect only when it runs as the top-level agent.
To let its calls run without prompts, put its allow rules in the parent agent's
`permissions.rules` or in a user or workspace `permissions.yaml`.
`--trust-all-tools` approves calls that would otherwise prompt; deny rules still
apply. In CLI 2.x, a subagent's own pre-approved tools run without a prompt even
when the parent does not allow them.

## CLI runtime behaviour

`Ctrl+G` opens the execution monitor for live per-subagent activity without
interrupting the main chat; `Ctrl+D` and `Ctrl+U` move between subagents and `q`
returns. `fs_read` inside the current working directory is auto-approved and
reads outside it still prompt. A non-interactive subagent cannot prompt, so it
fails fast when a tool needs approval unless the agent is in `trustedAgents`.
Each persisted subagent session records the spawning session's ID. With workflows
on, delegation to a custom agent runs as a background workflow run you can watch
under Workflows while the parent conversation continues; with workflows off, the
main agent waits for every subagent to finish.
