# Creating Custom Agents

Source: https://kiro.dev/docs/custom-agents/creating/ (fetched 2026-09-06;
upstream retired `/docs/cli/custom-agents/creating/`)

## Quick start

From chat, where `/agent generate` is an alias of `/agent create` and the default
mode generates the config from your description:

```
> /agent create
> /agent create backend-specialist -D "Backend coding specialist" -m code-analysis
```

Or from the terminal:

```bash
kiro-cli agent create backend-specialist
```

Without `--directory` the agent is saved globally to `~/.kiro/agents/`.

### Options

| Flag | Description | Availability |
|---|---|---|
| `--directory` | `workspace`, `global` (default), or a custom path | both |
| `--from` | template agent to base the new one on, implying `--manual` | both |
| `--description` | description of the agent | slash command |
| `--mcp-server` | MCP server to include, repeatable | slash command |
| `--manual` | open an editor instead of generating | slash command |

`--description` and `--mcp-server` belong to AI-assisted mode and cannot be
combined with `--manual` or `--from`.

## Agent configuration file

```json
{
  "name": "my-agent",
  "description": "A custom agent for my workflow",
  "tools": ["read", "write"],
  "allowedTools": ["read"],
  "resources": [
    "file://README.md",
    "file://.kiro/steering/**/*.md",
    "skill://.kiro/skills/**/SKILL.md"
  ],
  "prompt": "You are a helpful coding assistant",
  "model": "claude-sonnet-4"
}
```

## Using your agent

```bash
# Swap at runtime
> /agent swap

# Start with specific agent
kiro-cli --agent my-agent
```

Agents stored globally in `~/.kiro/agents/` or per-workspace in `.kiro/agents/`.
A new session starts on the default agent, `kiro_default`. A workspace agent
takes precedence over a global one of the same name, with a warning.
