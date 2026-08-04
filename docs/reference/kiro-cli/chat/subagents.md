# Subagents

Source: https://kiro.dev/docs/cli/chat/subagents/

Specialized agents that autonomously execute complex tasks with their own context and tool access.

## Key capabilities

- Autonomous execution with live progress tracking
- Core tool access: read, write, shell, code, MCP tools
- Parallel execution, result aggregation

## Available tools in subagents

**Available:** `read`, `write`, `shell`, `code`, MCP tools

**Not available:** `web_search`, `web_fetch`, `introspect`, `thinking`, `todo_list`, `use_aws`, `grep`, `glob`

## Configuring subagent access

### Restrict available agents

```json
{
  "toolsSettings": {
    "subagent": {
      "availableAgents": ["reviewer", "tester", "docs-*"]
    }
  }
}
```

### Trust specific agents (no permission prompts)

```json
{
  "toolsSettings": {
    "subagent": {
      "trustedAgents": ["reviewer", "tester"]
    }
  }
}
```

Glob patterns supported. Combine both for fine-grained control.
