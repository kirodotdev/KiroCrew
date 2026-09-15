# Custom Agents

Source: https://kiro.dev/docs/custom-agents/ (fetched 2026-09-06; upstream
retired `/docs/cli/custom-agents/` and made the page surface-agnostic)

Customize Kiro behavior by defining specific configurations for different use cases: which tools are available, permissions, and context.

## Benefits

- Pre-approve specific tools (no permission prompts)
- Limit tool access to reduce complexity
- Auto-include relevant context (project files, docs)
- Share configs with team members
- Works with both built-in tools and MCP tools

## Two config formats

JSON and Markdown carry identical fields; Markdown puts the config in front
matter and the system prompt in the body, which suits a long prompt. Both live at
`.kiro/agents/<name>.{json,md}` for a workspace or `~/.kiro/agents/` globally,
with the workspace copy winning a name collision. A workspace agent loads only if
the workspace is trusted.

Nested directories work, and the name is the extension-less path relative to the
agents directory, so `~/.kiro/agents/team/planner.md` is the agent `team/planner`.

## Backward compatibility

An existing 2.x JSON config keeps working unchanged. The fields added in CLI 3.0
— `permissions`, `excludedTools`, `includeMcpJson`, `resources` with `skill://`,
and the Markdown format — are all optional.
