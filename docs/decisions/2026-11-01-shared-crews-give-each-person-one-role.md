# A shared Crew gives each admitted person one role, once the operator turns roles on

Decided by: Bolin Chen (@bolichen97), maintainer
Date: 2026-11-01

## Decision
When the operator creates `people_policy.json`, each admitted person resolves to one role (owner, member, viewer, guest, or one the operator defines) that sets their dashboard rights, narrows their agent through a governance profile, and names the account their turns run as; with no file, every admitted person keeps the equal rights Shared Crew gives them today.

## Why
Shared Crew admits a person or refuses them, and everyone admitted can do everything. A shared host needs a way to let someone watch without acting, or act without changing Settings. Keeping roles off until the file exists means an existing Crew, Tailscale invites included, does not change.

## Evidence
- Maintainer decision: https://github.com/kirodotdev/KiroCrew/pull/27197#issuecomment-6561212426
- RFC: `docs/request-for-change/rfc-per-person-execution.md`, §7 and §12 decisions 5 and 6, merged in https://github.com/kirodotdev/KiroCrew/pull/27023
- Tracking issue: https://github.com/kirodotdev/KiroCrew/issues/27006
