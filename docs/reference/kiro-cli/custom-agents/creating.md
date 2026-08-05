# Creating Custom Agents

Source: https://kiro.dev/docs/cli/custom-agents/creating/

## Quick start

From chat:

```
> /agent generate
```

Or CLI:

```bash
kiro-cli agent create --name my-agent
```

Creates config at `~/.kiro/agents/my-agent.json`.

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
