# The first-run Get Kiro CLI card says only that Kiro CLI isn't installed, never that it is the default agent

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-14

## Decision

While Kiro CLI is not installed, the first line of the Get Kiro CLI card on first-run setup reads "Kiro CLI isn't installed on this host yet." in every language, with no claim about which agent is the default, and the card stays on the screen whatever agent is configured.

## Why

- The card does not know which agent is configured, and on a standard install that agent is Kiro Agent, so "Kiro CLI is the default agent" contradicted the "Use other coding agents" notice on the same screen, which names the configured agent.
- The card stays because the screen still offers Kiro CLI as a way to finish setup ("Use Kiro CLI instead"); hiding it when the configured agent does not need Kiro CLI would need a new host check and a different first-run screen.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/34678#issuecomment-6876070957 -- the maintainer's decision.
- https://github.com/kirodotdev/KiroCrew/issues/34678 -- the report that the setup screen named two default agents.
