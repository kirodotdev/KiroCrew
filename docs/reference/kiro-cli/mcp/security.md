# MCP Security

Source: https://kiro.dev/docs/cli/mcp/security/

Security model principles: explicit permission, local execution, isolation (separate processes), transparency.

## Best practices

- Only install MCP servers from trusted sources
- Review tool descriptions before installation
- Use least-privilege for server permissions
- Limit file system and network access
- Use environment variables for credentials (never hardcode)
- Rotate credentials regularly
- Use HTTPS for remote servers, verify SSL/TLS
- Review MCP server logs regularly
- Remove unused/untrusted servers promptly

```bash
# Use env vars for sensitive data
export MCP_API_KEY="your-secure-key"
kiro-cli mcp add my-server --env MCP_API_KEY
```
