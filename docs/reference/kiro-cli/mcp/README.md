# Model Context Protocol (MCP)

Source: https://kiro.dev/docs/mcp/ (fetched 2026-09-06; upstream retired
`/docs/cli/mcp/` and made the page surface-agnostic)

## In this section

| Page | Covers |
|---|---|
| [configuration.md](configuration.md) | Full `mcp.json` and agent-level MCP configuration reference. |
| [examples.md](examples.md) | Worked server configurations. |
| [security.md](security.md) | Trust and permission considerations for MCP servers. |
| [oauth-token-storage.md](oauth-token-storage.md) | Where an OAuth-authenticated server's tokens are stored. |

MCP extends Kiro's capabilities by connecting to specialized servers that provide additional tools and context. Use `/mcp` in chat to see loaded servers.

## Setup

### Command line

```bash
kiro-cli mcp add \
  --name "awslabs.aws-documentation-mcp-server" \
  --scope global \
  --command "uvx" \
  --args "awslabs.aws-documentation-mcp-server@latest" \
  --env "FASTMCP_LOG_LEVEL=ERROR"
```

### mcp.json file

Workspace: `<project-root>/.kiro/settings/mcp.json`
User: `~/.kiro/settings/mcp.json`

```json
{
  "mcpServers": {
    "server-name": {
      "command": "command-to-run",
      "args": ["arg1", "arg2"],
      "env": { "KEY": "value" },
      "disabled": false
    }
  }
}
```

### Agent configuration

```json
{
  "name": "myagent",
  "mcpServers": {
    "fetch": { "command": "fetch3.1", "args": [] }
  },
  "includeMcpJson": false
}
```

`includeMcpJson: true` includes workspace + user level MCP configs.

## Troubleshooting

- Tool name must be ≤64 chars including the server prefix, match
  `^[a-zA-Z][a-zA-Z0-9_]*$`, and carry a non-empty description
- A description over 10,000 chars still works but warns and may slow responses
