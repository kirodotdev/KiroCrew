# The KAS backend keeps remote MCP sign-ins across sessions by default, in the gateway's own vault

Decided by: Ray (project owner, @buluoray)
Date: 2026-11-02

## Decision

`agent.kas_persist_mcp_sign_in` is on by default, an install that stored the old default `false` is moved to `true` once, and the grant vault stays the gateway's (every session on the gateway reuses a sign-in), with no governance-ceiling gate; a stored `false` set after that one move, any non-`true` value, and a config read that fails or degrades keep sign-ins in memory.

## Why

- An install that never picked a backend now runs KAS, where each new session asks for every remote MCP sign-in again (Atlassian, Notion, Figma), and the opt-in was found by almost nobody.
- The `kiro` backend has always kept the same sign-ins host-wide in `~/.aws/sso/cache`, for every session and every person on the gateway, whatever governance is installed; this restores that behaviour on KAS rather than adding a new one.
- Shared Crew gives every admitted person the same rights over everything the Crew can do, so a per-person vault would not narrow what a collaborator can already reach. Per-user grants (a stricter, separate change) remain possible on top of this default.
- The one-time move of a stored `false` is Ray's explicit choice over a report-only row: every settings save since v0.11.0 wrote `false` into `config.json`, so without it the fix would barely reach existing users. `false` was the default until now (the setting read "When false (the default)"), so the value an owner had reason to write was `true`; a `false` set again after the move is kept.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/23135 -- keep KAS remote MCP sign-ins across sessions by default.
- https://github.com/kirodotdev/KiroCrew/issues/26182 -- Atlassian asks to sign in again on KAS after updating.
- https://github.com/kirodotdev/KiroCrew/pull/22625 -- the opt-in key this turns on.
- `docs/reference/kiro-cli/mcp/oauth-token-storage.md` -- where the `kiro` backend keeps the same sign-ins.
- `docs/system-specs/modules/shared-crew.md` §1 -- every admitted collaborator has the same rights.
