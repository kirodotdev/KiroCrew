# The Tailscale serve port is a setting, and the trusted tailnet origin carries that port

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-26

## Decision

`dashboard.tailscale.https_port` (default 443) is the one port Kiro Crew publishes on, reads and withdraws with `tailscale serve`; when it is not 443 the dashboard trusts `https://<name>:<port>` in place of `https://<name>`, so there is still exactly one tailnet origin, under `dashboard.tailscale.enabled` and `capabilities.tailnet_origin`, and Kiro Crew neither detects nor adopts a serve handler on another port.

## Why

- The maintainer's ruling of 2026-11-26 on the issue: one setting, used by `kirocrew tailnet status`/`up`/`down`, `kirocrew token` and the Phone access card, with the running dashboard's trusted origin following it from its next start; a sign-in link is only handed out when the handler on that port is this dashboard.
- Port 443 was pinned only so that the derived origin, which had no port, matched the browser's `Origin`; deriving the origin from the same setting removes that reason.
- The port replaces the bare origin instead of being added beside it, so a host that moves the dashboard off 443 stops trusting whatever else serves 443 on the same name.
- A value that is not a port number reads as 443, the default, so a typo adds no refusal and is no wider than before the setting existed.
- The origin still comes from explicit configuration only: inferring it from what `tailscale serve` already proxies, and offering to adopt such a handler, is a second path the setting makes unnecessary.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/40663#issuecomment-7183938015 -- the maintainer's 2026-11-26 decision.
- https://github.com/kirodotdev/KiroCrew/issues/40663 -- the issue recording the request.
