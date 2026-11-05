# Turning the KAS sign-in setting off stops stored sign-ins being used but does not erase them

Decided by: Ray (@buluoray, repo admin)
Date: 2026-11-03
Supersedes: 2026-11-02-kas-keeps-remote-mcp-sign-ins-by-default.md

## Decision

`agent.kas_persist_mcp_sign_in` stays on by default with the one-time move of a stored `false` to `true`, the gateway-wide vault and no governance-ceiling gate, as the superseded entry records; turning the setting off stops KAS from reading or writing the stored grants but leaves them in `kas/mcp-grants/`, and erasing them stays a manual step (delete that directory).

## Why

- Silently wiping sign-ins when the setting reads off would confuse users: every remote MCP server would ask to sign in again with no visible cause.
- With the setting off, the vault is not read at all (the handshake declares no `secretStorage`), so grants left on disk change nothing a user sees until the setting is turned back on.
- This entry also corrects the superseded entry's reason for the one-time move. That entry says every settings save since v0.11.0 wrote `false`; in the code, the Settings page's save and `kirocrew config set` write only the key being set. The key was added in #22625 (first in v0.11.0-rc.1) with default `false` and is not in the Settings UI, and the one whole-config write, `kirocrew config edit` on a home with no `config.json`, writes every field with its default. A stored `false` is that written-out default or a manual edit, and almost never a choice made in the UI, so the one-time move stays.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/28630#issuecomment-6592528548 -- Ray's decision on default-on and the one-time move.
- https://github.com/kirodotdev/KiroCrew/pull/28630#pullrequestreview-5747203530 -- the maintainer review that asked for off to erase the stored grants.
- https://github.com/kirodotdev/KiroCrew/pull/28630#issuecomment-6595657326 -- the facts behind where a stored `false` comes from.
