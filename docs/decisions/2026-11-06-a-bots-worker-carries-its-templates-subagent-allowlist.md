# A Bot's worker carries its template's sub-agent allowlist

Decided by: @bolichen97 (maintainer)
Date: 2026-11-06
Supersedes: 2026-11-02-a-crewmates-worker-carries-its-template-policy.md

## Decision

Everything in `2026-11-02-a-crewmates-worker-carries-its-template-policy.md` stands, with one change to its `toolsSettings` allowlist: `subagent.availableAgents` is carried too, spelled under the `subagent` key only and only when KAS can apply every entry, and the Bot's worker is held to it on both spawn paths, KAS's own sub-agent tool and Crew's `spawn_run` / `spawn_sub_agents`; `subagent.trustedAgents`, the `use_subagent` spelling and every other key outside the allowlist are still refused with their names.

## Why

- Many published agents declare a sub-agent allowlist, so none of them could be added to a Bot, and the only workaround was a hand-edited copy with the list removed, which drops the restriction and goes stale on every update.
- The list only narrows what the worker may spawn, so carrying it keeps the earlier rule that the worker never runs wider than the agent declares. Dropping it with a warning instead would let the worker spawn any agent, which is wider.
- `trustedAgents` grants prompt-free runs rather than narrowing anything, so it stays refused until someone decides it separately.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/31292#issuecomment-6692569737 -- the maintainer's statement of this decision.
- https://github.com/kirodotdev/KiroCrew/issues/31275 -- a Bot refuses an agent that declares a sub-agent allowlist.
- https://github.com/kirodotdev/KiroCrew/pull/31292 -- the pull request that adds this entry and implements it.
- https://github.com/kirodotdev/KiroCrew/pull/28501 -- the pull request that set the allowlist this entry extends.
