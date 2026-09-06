# MCP Configuration

Source: https://kiro.dev/docs/mcp/configuration/ (fetched 2026-09-06; upstream
retired `/docs/cli/mcp/configuration/` and made the page surface-agnostic)

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
| `args` | Array | No | Arguments |
| `env` | Object | No | Environment variables (`${VAR}` expansion) |
| `disabled` | Boolean | No | Default false |
| `autoApprove` | Array | No | Tools to auto-approve; `"*"` approves all |
| `disabledTools` | Array | No | Tools to omit |

## Remote server properties

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `url` | String | Yes | HTTPS endpoint (HTTP for localhost) |
| `headers` | Object | No | Connection headers |
| `env` | Object | No | Environment variables for the server process |
| `oauth` | Object | No | OAuth configuration for a server that needs authentication |
| `oauthScopes` | Array | No | Scopes to request; `oauth.oauthScopes` overrides this |
| `disabled` | Boolean | No | Default false |
| `autoApprove` | Array | No | Tools to auto-approve; `"*"` approves all |
| `disabledTools` | Array | No | Tools to omit |

## Loading priority (highest → lowest)

1. Agent Config (`mcpServers` in agent JSON)
2. Workspace MCP JSON (`.kiro/settings/mcp.json`)
3. Global MCP JSON (`~/.kiro/settings/mcp.json`)

Same-name servers: higher priority wins, so an agent-config entry replaces the
lower ones outright and `disabled: true` there launches nothing. Different names:
additive.

## Environment variables

`${VAR}` in `env` expands from the shell, so export the value before starting the
CLI. Variable expansion needs no approval step on the CLI; the approval list is
an IDE control.

## OAuth authentication

A remote server behind OAuth is handled with a browser flow on connect. Servers
without Dynamic Client Registration take your own credentials in `oauth`; the CLI
supports a confidential client, so `clientSecret` works there where the IDE is
PKCE-only.

```json
{
  "mcpServers": {
    "remote-server-with-oauth": {
      "url": "https://endpoint.example.com/mcp",
      "oauth": {
        "clientId": "...",
        "clientSecret": "...",
        "redirectUri": "http://localhost:7778/oauth/callback",
        "oauthScopes": ["files:read"]
      }
    }
  }
}
```

| Property | Description |
|---|---|
| `oauth.clientId` | Pre-registered client ID; setting it skips DCR entirely |
| `oauth.clientSecret` | For a server that requires one, meaningful only with `clientId` |
| `oauth.redirectUri` | Custom loopback callback; pin the port and path when the app pre-registered one |
| `oauth.clientMetadataUrl` | HTTPS URL of a hosted Client ID Metadata Document, used as the client identity instead of registering; falls back to DCR when the authorization server does not advertise support |
| `oauth.oauthScopes` | Scopes to request, taking priority over the top-level `oauthScopes` |

A scope error is often fixed with an empty `"oauthScopes": []`. Pair
`clientMetadataUrl` with a pinned `redirectUri`: the default loopback port is
random, and under CIMD the authorization server validates the callback against
the document's `redirect_uris`, so an unpinned callback is rejected every connect.

## Disabling

```json
{ "disabled": true }                                // disable server
{ "disabledTools": ["delete_file", "execute_cmd"] } // disable specific tools
```

## View loaded servers

```bash
/mcp
```
