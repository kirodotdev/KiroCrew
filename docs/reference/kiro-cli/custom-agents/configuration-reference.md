# Agent Configuration Reference

Source: https://kiro.dev/docs/cli/custom-agents/configuration-reference/

## File locations

- Local (project): `.kiro/agents/` — local takes precedence
- Global (user): `~/.kiro/agents/`

Filename (without `.json`) becomes the agent name.

## All fields

| Field | Description |
|-------|-------------|
| `name` | Agent identifier |
| `description` | Human-readable purpose |
| `prompt` | System prompt (inline string or `file://` URI) |
| `mcpServers` | MCP servers the agent can access |
| `tools` | Available tools list |
| `toolAliases` | Tool name remapping |
| `allowedTools` | Tools that skip permission prompts |
| `toolsSettings` | Per-tool configuration |
| `resources` | Files, skills, knowledge bases |
| `hooks` | Lifecycle commands |
| `includeMcpJson` | Include MCP servers from mcp.json files |
| `model` | Model ID (e.g. `claude-sonnet-4`) |
| `keyboardShortcut` | Quick-switch shortcut (e.g. `ctrl+a`) |
| `welcomeMessage` | Message shown on agent switch |

## prompt

Inline or file URI. Relative paths resolve from agent config file directory.

```json
{ "prompt": "You are an expert" }
{ "prompt": "file://./prompts/expert.md" }
```

## mcpServers

```json
{
  "mcpServers": {
    "git": {
      "command": "git-mcp",
      "args": [],
      "env": { "GIT_CONFIG_GLOBAL": "/dev/null" },
      "timeout": 120000
    }
  }
}
```

## tools

```json
{ "tools": ["read", "write", "shell", "@git", "@fetch/fetch_url"] }
{ "tools": ["*"] }        // all tools
{ "tools": ["@builtin"] } // all built-in only
```

- Built-in: `"read"`, `"shell"`, etc.
- All MCP server tools: `"@server_name"`
- Specific MCP tool: `"@server_name/tool_name"`

## toolAliases

Resolve naming collisions or create shorter names:

```json
{
  "toolAliases": {
    "@github-mcp/get_issues": "github_issues",
    "@gitlab-mcp/get_issues": "gitlab_issues"
  }
}
```

## allowedTools

Tools that run without permission prompts. Supports exact matches and glob patterns:

```json
{
  "allowedTools": [
    "read",                    // exact built-in
    "@git/git_status",         // exact MCP tool
    "@server/read_*",          // wildcard prefix
    "@fetch",                  // all tools from server
    "@git-*/*"                 // pattern server + all tools
  ]
}
```

Pattern rules: `*` matches any chars, `?` matches one char, case-sensitive.

## toolsSettings

Per-tool configuration:

```json
{
  "toolsSettings": {
    "write": { "allowedPaths": ["src/**", "tests/**"] },
    "shell": {
      "allowedCommands": ["git status"],
      "deniedCommands": ["git push .*"],
      "autoAllowReadonly": true
    },
    "subagent": {
      "availableAgents": ["reviewer", "tester"],
      "trustedAgents": ["reviewer"]
    }
  }
}
```

## resources

```json
{
  "resources": [
    "file://README.md",                      // loaded at startup
    "file://.kiro/steering/**/*.md",         // glob patterns
    "skill://.kiro/skills/**/SKILL.md",      // on-demand loading
    {
      "type": "knowledgeBase",               // indexed search
      "source": "file://./docs",
      "name": "ProjectDocs",
      "description": "Project documentation",
      "indexType": "best",
      "autoUpdate": true
    }
  ]
}
```

- `file://` — loaded into context at startup
- `skill://` — metadata at startup, full content on demand
- `knowledgeBase` — indexed for search (millions of tokens)

## hooks

```json
{
  "hooks": {
    "agentSpawn": [{ "command": "git status" }],
    "userPromptSubmit": [{ "command": "ls -la" }],
    "preToolUse": [{ "matcher": "execute_bash", "command": "audit.sh" }],
    "postToolUse": [{ "matcher": "fs_write", "command": "cargo fmt --all" }],
    "stop": [{ "command": "npm test" }]
  }
}
```

## keyboardShortcut

Format: `[modifier+]key`. Modifiers: `ctrl`, `shift`. Keys: `a-z`, `0-9`. Toggle behavior (press again to switch back).

## Complete example

```json
{
  "name": "aws-rust-agent",
  "description": "Specialized agent for AWS and Rust development",
  "prompt": "file://./prompts/aws-rust-expert.md",
  "mcpServers": {
    "fetch": { "command": "fetch-server", "args": [] },
    "git": { "command": "git-mcp", "args": [] }
  },
  "tools": ["read", "write", "shell", "aws", "@git", "@fetch/fetch_url"],
  "toolAliases": { "@git/git_status": "status" },
  "allowedTools": ["read", "@git/git_status"],
  "toolsSettings": {
    "write": { "allowedPaths": ["src/**", "tests/**", "Cargo.toml"] }
  },
  "resources": ["file://README.md", "file://docs/**/*.md"],
  "hooks": {
    "agentSpawn": [{ "command": "git status" }],
    "postToolUse": [{ "matcher": "fs_write", "command": "cargo fmt --all" }]
  },
  "model": "claude-sonnet-4",
  "keyboardShortcut": "ctrl+shift+r",
  "welcomeMessage": "Ready to help with AWS and Rust development!"
}
```
