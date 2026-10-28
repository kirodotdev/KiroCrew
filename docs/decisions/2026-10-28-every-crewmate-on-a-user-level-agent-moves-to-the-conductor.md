# Every crewmate still bound to a user-level agent moves onto its conductor, and a restriction its file cannot carry withholds the tools it guarded

Decided by: Ray (project owner, @buluoray)
Date: 2026-10-28
Supersedes: 2026-10-27-every-used-crewmate-moves-to-the-conductor.md

## Decision

The startup migration moves every crewmate still bound to an agent spec in the user-level agents folder onto its conductor and its own crewmate file: a loss moves with the one-time notice, a grant the worker would gain is never written and is offered only through the capability card, and a `hooks`, `permissions` or `toolsSettings` restriction the file cannot carry no longer keeps the crewmate where it is but fails closed, withholding every tool it guarded and naming that in the notice; a crewmate whose id is shadowed by a Kiro Crew-owned private copy moves after that copy is retired, one shadowed by a spec Kiro Crew does not own stays and is told once, a used row with no `member_id` is given one and its own V2 store before it moves, and the shared-template governance pass never writes a spec Kiro Crew does not own.

## Why

- A row left on a user-level agent made the shared-template governance pass rewrite the user's own file, which kiro-cli also loads outside Kiro Crew, so the user's own sessions lost their auto-approvals and a package manager reinstalling the spec fought the rewrite on every restart.
- Withholding the tools a dropped restriction guarded leaves the worker able to do less than before, never more, which keeps the earlier entry's safety rule: nothing the user never allowed is applied without their approval.
- This replaces only the earlier entry's rule that a hook or a `permissions` / `toolsSettings` restriction keeps the crewmate where it is; its rules on losses, gains and unreadable agents stand.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/25289 -- the reported rewrite of a bound package spec.
- https://github.com/kirodotdev/KiroCrew/pull/25325 -- the pull request that implements this entry.
