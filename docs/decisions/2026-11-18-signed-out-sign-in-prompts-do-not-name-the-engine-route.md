# Signed-out sign-in prompts no longer name the engine route

Decided by: Zezhen Xu (maintainer, @CrysisDeu)
Date: 2026-11-18
Supersedes: 2026-10-30-choosing-kas-runs-kiro-crews-own-engine-when-kiro-cli-cannot.md

## Decision

Signed-out sign-in prompts no longer name the engine route; the route stays shown in Settings and `kirocrew doctor`, and the switch stays logged. On kiro-cli's relay, a signed-out chat shows the welcome screen's kiro-cli login commands and, after "or", the Kiro Crew sign-in button; on the bundled engine it shows the Sign in button. This replaces only the clause of the 2026-10-30 entry that every KAS prompt names the real route and points to the Kiro sign-in rather than `kiro-cli login`; the rest of that entry stands.

## Why

- The owner chose one plain line and one action for every signed-out spot (Layout 1), and asked that the relay row teach kiro-cli's sign-in the way the welcome screen does.
- A live test showed that on kiro-cli 3.x the relay runs on Kiro Crew's sign-in when the vault holds one and on kiro-cli's otherwise, so a relay prompt that named only the Kiro sign-in pointed half its users at the wrong fix.
- The route is still visible where a person looks for it on purpose: Settings, `kirocrew doctor` and the gateway log.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/36888#issuecomment-6972652853 -- Zezhen's owner decision on the pull request that adds this entry.
- https://github.com/kirodotdev/KiroCrew/pull/36888 -- the pull request that adds this entry and implements it.
