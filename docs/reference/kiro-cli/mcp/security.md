# MCP Security

Source: https://kiro.dev/docs/mcp/security/ (fetched 2026-09-06; upstream retired
`/docs/cli/mcp/security/`, and titles the page "Best practices")

Security model principles: explicit permission, local execution, isolation (separate processes), transparency.

## What an MCP server can reach

Upstream states plainly that a stdio MCP server executes arbitrary commands in
your environment with the same privileges and access as the agent itself. The
`command` and `args` run as a process you own; the server has full workspace
filesystem access, can read the session's environment variables and secrets, and
runs **outside** the agent's tool-execution sandbox, so it is not bound by the
restrictions that apply to agent tool calls. A compromised server can therefore
exfiltrate code and credentials with no further confirmation, and upstream does
not vet, sandbox or restrict third-party servers.

Kiro Crew's own controls over this are separate and govern here; see
[security](../../../system-specs/modules/security.md).

## Best practices

- Only install MCP servers from trusted sources, and review the source first
- Review tool descriptions before installation
- Use least-privilege for server permissions
- Limit file system and network access
- Use `disabledTools` to keep dangerous operations out of reach
- Use environment variables for credentials (never hardcode)
- Restrict permissions on `mcp.json` itself, at both user and workspace level
- Rotate credentials regularly, and store them in a system keychain
- Use HTTPS for remote servers, verify SSL/TLS
- Review MCP server logs regularly
- Remove unused/untrusted servers promptly

```bash
# Use env vars for sensitive data
export MCP_API_KEY="your-secure-key"
kiro-cli mcp add my-server --env MCP_API_KEY
```
