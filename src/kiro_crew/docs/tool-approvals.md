# Tool approvals: auto-approve one MCP tool so it stops asking

In Normal mode Kiro Crew asks you to authorize each MCP tool call. On the KAS
engine you can always allow one tool, for example a Slack server's search or
read tools, so it stops asking while every other tool keeps asking. This page
shows the place that works (an `allow` rule in `permissions.yaml`), and the
settings that look like they should auto-approve a tool (`autoApprove`,
`allowedTools`) but do not.

## Auto-approve an MCP tool on KAS

KAS reads its permission rules from one file in your home folder:

| System | File |
|---|---|
| macOS, Linux | `~/.kiro/settings/permissions.yaml` |
| Windows | `%USERPROFILE%\.kiro\settings\permissions.yaml` |

Create the file if it does not exist, and add one `allow` rule for `mcp`:

```yaml
rules:
  - capability: mcp
    effect: allow
    match:
      - slack/search
      - slack/get_messages
      - slack/get_thread
```

- Each `match` entry is `<server>/<tool>`, with no leading `@`. The server is
  the MCP server's name as listed under **MCP Servers** (`/mcp`); the tool is
  the tool's own name.
- `*` matches any characters: `slack/get_*` allows every tool whose name starts
  with `get_`. `slack/*` allows the whole server, write tools included, so list
  read tools one by one instead.
- KAS rereads the file when it changes, so no restart is needed.
- A `deny` rule anywhere wins over an `allow`. Rules with `effect: ask` keep
  asking.
- `~/.kiro/settings/permissions.json` with the same `{"rules": [...]}` shape
  works too. When both exist, a non-empty `permissions.yaml` is the one read.
- If the file does not parse, KAS ignores its rules and every tool asks again.
  Check the indentation first.

## Add the rule yourself

A rule in `permissions.yaml` lets a tool run without asking you, so it is your
decision to make. Ask the agent for the exact lines, then add them to the file
yourself.

## What does not stop the approval prompts

- **`allowedTools` in `~/.kiro/agents/kirocrew.json`.** Kiro Crew writes that file
  itself and rewrites it on every gateway start, so a hand edit is lost.
- **`autoApprove` on a server in `mcp.json`.** Kiro Crew can withhold it, for
  example under your organization's policy or for a server it shares through its
  own MCP gateway. A withheld grant is recorded only in Kiro Crew's security
  event log (`mcp_auto_approve_withheld`); nothing in the chat says so, and the
  tool keeps asking.
- **Trust mode** stops every prompt, not one tool's. Use it only when that is
  what you want.

To change what a server can do at all, rather than whether it asks, use
**MCP Servers** (`/mcp`); see [Connections](connections.md).
