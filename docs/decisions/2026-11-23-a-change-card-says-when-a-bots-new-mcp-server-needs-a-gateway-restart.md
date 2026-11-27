# A change card says when a Bot's new MCP server needs a gateway restart

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-23

## Decision

When a Bot's capabilities change card adds or changes a server the gateway shares (one named in `mcp_gateway.stub_servers`) on a Bot that owns its file, the save runs the gateway's existing overlay refresh, so the Bot's worker gets the server through the gateway at once when the running gateway already serves that launch; when it does not, the overlay is left as it was and the applied card shows one line: "This Bot's worker gets this MCP server through the gateway only after you restart the gateway (Settings → About → Restart gateway)."

## Why

- The ruling chose the existing refresh and a line on the card over admitting a new launch live or restarting anything: the gateway's daemon reads its launch targets once, when it starts, so a refresh that published a stub for a launch it does not hold would route the worker to nothing.
- The line says only that the person needs to restart the gateway, and where. It does not promise the server works after one, because a launch nobody approved stays unwrapped until it is approved.
- The line is read from the card's own record, so a page reload shows it and nothing else carries it. The "Add abilities" path, the Abilities pane's approvals and removals, and every other card kind are left as they are.

## Evidence

- The ruling: https://github.com/kirodotdev/KiroCrew/issues/40840#issuecomment-7091747788
- The report: https://github.com/kirodotdev/KiroCrew/issues/40840
