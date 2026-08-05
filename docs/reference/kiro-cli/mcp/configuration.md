# MCP Configuration

Source: https://kiro.dev/docs/cli/mcp/configuration/

## Config structure

```json
{
  "mcpServers": {
    "local-server": {
      "command": "cmd",
      "args": ["arg1"],
      "env": { "KEY": "${EXPANDED_VAR}" },
      "disabled": false,
      "disabledTools": ["tool_name"],
      "autoApprove": ["tool_name"]
    },
    "remote-server": {
      "url": "https://endpoint.example.com",
      "headers": { "Authorization": "Bearer ${TOKEN}" },
      "disabled": false
    }
  }
}
```

## Local server properties

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `command` | String | Yes | Command to run the server |
| `args` | Array | Yes | Arguments |
| `env` | Object | No | Environment variables (`${VAR}` expansion) |
| `disabled` | Boolean | No | Default false |
| `autoApprove` | Array | No | Tools to auto-approve |
| `disabledTools` | Array | No | Tools to omit |

## Remote server properties

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `url` | String | Yes | HTTPS endpoint (HTTP for localhost) |
| `headers` | Object | No | Connection headers |

## Loading priority (highest → lowest)

1. Agent Config (`mcpServers` in agent JSON)
2. Workspace MCP JSON (`.kiro/settings/mcp.json`)
3. Global MCP JSON (`~/.kiro/settings/mcp.json`)

Same-name servers: higher priority wins. Different names: additive.

## Disabling

```json
{ "disabled": true }                                // disable server
{ "disabledTools": ["delete_file", "execute_cmd"] } // disable specific tools
```

## View loaded servers

```bash
/mcp
```
