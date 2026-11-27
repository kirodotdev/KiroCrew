# Add abilities leaves a named permission bundle behind and carries the rules beside it

Decided by: Ray Xu (buluoray), Kiro Crew maintainer
Date: 2026-11-20
Supersedes: 2026-11-07-add-abilities-leaves-out-a-grant-the-worker-already-holds.md (in part: its "Options not chosen" item "drop named permission bundles", at Add abilities time only)

## Decision

When a Bot adds abilities from an agent whose `permissions` block holds `rules` plus a `policies` list of bundle ids and nothing else, the add leaves the bundle out and carries the rules beside it, deny rules included, the same way the upgrade move reads such a block; the calls the bundle would have allowed then ask instead of running unasked, and every other part of the 2026-11-07 entry stands.

## Why

- A user could not give a Bot an installed agent's abilities: every agent whose `permissions` name a bundle was refused with "permissions can't be kept exactly", and many packaged agents ship `"policies": ["read-only-shell"]` beside their rules.
- The 2026-11-07 entry rejected dropping bundles because Crew's ceiling cannot see a bundle's denies. The bundles KAS ships hold no deny or ask rule: the preset table in the bundled `@kiro/agent` `dist/server/acp-server.js` (the `new Map` of presets) lists seven bundles in KAS 3.2.1, 3.3.0, 3.4.0 and 3.5.0 (`allow-all`, `edit-workspace`, `dev-shell`, `approve-for-me`, `read-all`, `read-only-shell`, `read-workspace`), and every rule in them, in those versions and in 3.1.0 and 3.2.0, has effect `allow` or `evaluate` (`evaluate` only in `approve-for-me`, which applies only with the trust classifier on). `read-only-shell` is nine `shell` allow rules for read-only commands.
- So leaving a bundle out only narrows the worker: what the bundle allowed asks first.
- The upgrade move already reads a bundle beside rules as the rules alone (`mate_held_rules._bundle_beside_rules`); Add abilities now uses the same check, so the two paths agree.
- Follow-up, not part of this decision: once KAS exposes how it expands a bundle, Crew expands the bundle and filters each rule against the governance ceiling instead of dropping it.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/40131 -- the pull request that carries this behaviour; Ray Xu (buluoray), a maintainer listed in `MAINTAINERS.md`, states the decision in a comment on it.
- https://github.com/kirodotdev/KiroCrew/issues/40128 -- the user-reported refusal this decision answers.
- https://github.com/kirodotdev/KiroCrew/pull/39543 -- the pull request that made the upgrade move read a bundle beside rules as the rules alone.
