# MCP Examples

Source: https://kiro.dev/docs/cli/mcp/examples/

## AWS Documentation server

Prerequisites: `uv` from Astral, Python 3.10+.

```json
{
  "mcpServers": {
    "aws-docs": {
      "command": "uvx",
      "args": ["awslabs.aws-documentation-mcp-server@latest"],
      "env": { "FASTMCP_LOG_LEVEL": "ERROR" }
    }
  }
}
```

Tools: `search_documentation`, `read_documentation`, `recommend`.

## GitHub MCP server

Uses official Docker-based server (the npm `@modelcontextprotocol/server-github` is archived).

Prerequisites: Docker, GitHub Personal Access Token (fine-grained).

```json
{
  "mcpServers": {
    "github": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-e", "GITHUB_PERSONAL_ACCESS_TOKEN",
        "ghcr.io/github/github-mcp-server"
      ],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "your-token-here"
      }
    }
  }
}
```

Tools: `search_repositories`, `list_branches`, `list_issues`, `update_issue`, `add_issue_comment`, `create_pull_request`.

Toolsets can be filtered via `GITHUB_TOOLSETS` env var.

## Custom MCP servers

1. Choose language (Python, Node.js, etc.)
2. Implement MCP protocol
3. Define tools and package

Resources: [MCP Spec](https://modelcontextprotocol.io/specification/2025-06-18), [MCP Registry](https://github.com/modelcontextprotocol/registry)
