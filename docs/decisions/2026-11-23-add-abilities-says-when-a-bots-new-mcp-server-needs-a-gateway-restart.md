# Add abilities says when a Bot's new MCP server needs a gateway restart

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-23

## Decision

When **Add abilities** copies a server the gateway shares (one named in `mcp_gateway.stub_servers`) into a Bot's own file, the outcome is the one a capabilities change card has: the Bot's worker gets the server through the gateway with no restart when the running gateway already serves that launch, and the page says a gateway restart is needed when it does not.

## Why

- The change card's entry (`2026-11-23-a-change-card-says-when-a-bots-new-mcp-server-needs-a-gateway-restart.md`) left the Add abilities path as it was on purpose; the same gap on that path was filed as its own follow-up, with the card's outcome as the expected behaviour.

## Evidence

- The ruling: https://github.com/kirodotdev/KiroCrew/issues/41361 (its "Expected" section)
- The change card's outcome it matches: https://github.com/kirodotdev/KiroCrew/issues/40840#issuecomment-7091747788
