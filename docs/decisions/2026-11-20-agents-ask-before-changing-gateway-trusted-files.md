# Agents ask before changing the files the gateway trusts; only the person's click writes them

Decided by: Ray Xu (maintainer, @buluoray)
Date: 2026-11-20

## Decision

Agents never change the gateway's important files directly (MCP registries,
agent specs, config, approvals, other files the gateway launches from or
trusts). They ask through a tool that puts a card in front of the user, and only
the user's click writes the change. Because only the user can change these
files, the gateway trusts their content and does not re-ask the user about
changes the user already applied.

The first application is the MCP registries. Kiro Crew's own MCP store
(`<data home>/mcp.json`) is refused to the agent's file tools and read-only in
every sandbox, and kiro-cli's `~/.kiro/settings/mcp.json` stays refused to the
file tools. A refused write names `request_server`, which now adds, changes or
removes a server through an `mcp.server` card that shows the change; the card's
Apply is the only writer. Turning on MCP server sharing from that card restarts the
broker only, never the gateway.

## Why

- Security rules that assume an agent can write these files make the user
  approve the same thing again and again. A shared MCP server asks again after
  every installer update, and that hurts the experience.
- When only the user's click can write a file, that click is the consent, so the
  gateway can trust the file afterwards instead of asking again.

## Scope of the first change

- `<data home>/mcp.json`: file-tool write tier, read-only sandbox seal, its
  gateway saves recorded for the launcher's pin.
- Both MCP registries: a refused write tells the agent to call `request_server`.
- `request_server`: `action` `add` (with `source` `custom` and the entry),
  `change` and `remove`, raising an `mcp.server` card with **Restart open chats
  to use it** (checked by default; a remove card reads **Restart open chats to
  stop using it**) and **Turn on MCP server sharing**.
  `propose_change` cannot raise that card or `mcp.add_custom`.
- A share applies by restarting the broker, never the gateway.
- The card asks no question about sharing. The asking agent says whether the server
  reads or writes the chat's folder (`uses_chat_folder`, required), and Kiro Crew
  checks the entry itself. If either says yes, or Kiro Crew cannot tell, the server
  is not shared and each chat runs its own copy; the sharing box shows only for a
  server that is safe to share.

## Follow-ups

Files an agent can still write today that the gateway launches from or trusts,
each with what would replace the direct write. None is built by this entry.

| File | Today | Replacement |
|---|---|---|
| kiro-cli's `~/.kiro/settings/mcp.json` | File tools refused; an agent shell can still write it (not OS-sealed) | Seal it and record kiro-cli's own saves; `request_server` is already the way to ask |
| A project's agent specs and MCP files (`.kiro/agents/*.json`, `.kiro/settings/mcp.json`, `.mcp.json` in a work folder or a `KIROCREW_PROJECT_DIR` checkout) | Writable in the workspace; specs feed the env-reference filter and the shared-server launch check | `template.update` and `request_server` cards |
| Shared-server launch approvals for names in those files | A changed launch is refused as `changed_needs_reapproval` and asks again | Once every source of a launch is user-only, the user's apply is the approval |
| Cron script bodies (`crons/`) | Agent-writable; the gateway runs them on a schedule (in the sandbox) | `schedule.create` / `schedule.update` cards carrying the script |
| `tags.json` rows and `session_map.json` mirror links | Agent-writable stores the gateway reads for status and routing | A card, or a gateway-side tool that writes them |
| An app's `index.json` | Agent-writable; read for the app's listing | The App Store or a `trust.app` card |

A later change that lets an agent write any such file directly cites this entry
and supersedes it.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/40308 -- the pull request that
  adds this entry, the `mcp.json` seal and the `request_server` actions.
- https://github.com/kirodotdev/KiroCrew/pull/40308#issuecomment-7057913394
  -- Ray Xu's comment on that pull request stating this decision.
