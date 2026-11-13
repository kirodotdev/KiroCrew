# Every crewmate still bound to a user-level agent moves onto its conductor, and a restriction its file cannot carry moves with it and is named in its notice

Decided by: Ray (project owner, @buluoray)
Date: 2026-10-28
Supersedes: 2026-10-27-every-used-crewmate-moves-to-the-conductor.md

## Decision

The startup migration moves every crewmate still bound to an agent spec in the user-level agents folder onto its conductor and its own crewmate file: a loss moves with the one-time notice; a grant the worker would gain is never written and is offered only through the capability card; a `hooks`, `permissions` or `toolsSettings` restriction the crewmate file cannot carry no longer keeps the crewmate where it is and withholds no tool, the crewmate moves as it is, and its one-time notice names each restriction that no longer applies; the governance ceiling still governs the merged worker spec; a crewmate whose id is shadowed by a Kiro Crew-owned private copy moves after that copy is retired, one shadowed by a spec Kiro Crew does not own stays and is told once; a used row with no `member_id` is given one and its own V2 store before it moves; and the shared-template governance pass never writes a spec Kiro Crew does not own.

## Why

- A row left on a user-level agent made the shared-template governance pass rewrite the user's own file, which kiro-cli also loads outside Kiro Crew, so the user's own sessions lost their auto-approvals and a package manager reinstalling the spec fought the rewrite on every restart.
- Keeping a crewmate on its old agent for a restriction its file cannot carry left the crewmate doing its work in its chat, the opposite of what the upgrade notice describes; the user is told which restrictions no longer apply, and the governance ceiling still bounds what the worker may do.
- This replaces only the earlier entry's rule that a hook or a `permissions` / `toolsSettings` restriction keeps the crewmate where it is; its rules on losses, gains and unreadable agents stand.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/25289 -- the reported rewrite of a bound package spec.
- https://github.com/kirodotdev/KiroCrew/pull/25325 -- the pull request that implements this entry.
