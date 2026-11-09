# Add abilities leaves out a grant-only tool setting the Bot's worker already grants

Decided by: Bolin Chen (bolichen97), Kiro Crew maintainer, 2026-11-07
Date: 2026-11-07
Supersedes: 2026-11-02-a-crewmates-worker-carries-its-template-policy.md (in part: "every other `toolsSettings` key, in any form, is refused with its name", for `web_fetch.trusted` and `subagent.trustedAgents` that the merged worker already grants, at Add abilities time only)

## Decision

When a Bot adds abilities from an agent, a grant-only `toolsSettings` key that the Bot's merged worker already grants is left out of the copy and shown as an "Already has" preview row instead of refusing the add: `web_fetch.trusted` when the worker's own `allowedTools` grant a bare `web_fetch` allow, and `subagent.trustedAgents` when every entry is a literal agent name that a carried `subagent` allow already names; this happens at Add abilities time only, the worker merge still refuses either key in a Bot's file, and every other part of the 2026-11-02 entry stands.

## Why

- Add abilities from Atlas was refused whole (its servers, skills and prompt) because of `web_fetch.trusted`. KAS never applies that key, and it grants nothing to a worker whose `allowedTools` already list `web_fetch`, so leaving it out keeps the worker behaving exactly as the agent declares: no prompt appears or disappears.
- At Add abilities time only, so a hand-edited Bot file that holds either key still fails closed at session start.
- It reuses the existing "skipped" preview row and its "Already has" label, so there is no new row kind and no new locale string.
- Only literal agent names are proved covered. A pattern in `trustedAgents`, and a `trustedAgents` entry no carried allow names, stay refused by name.
- The 2026-11-02 entry was decided by Bolin Chen and Ray Xu (buluoray); this entry changes only the part its Supersedes line names.
- Options not chosen: keep the refusal and only reword it (every new agent template with such a key would get the same refusal); drop grant-only keys with a new "not carried" row (it makes some workers narrower than their agent declares and needs a new row kind in every locale); carry the grants KAS enforces, such as `autoAllowReadonly`, `allowedPaths` and wider `trustedAgents` (more prompt-free runs on a worker nobody watches; left to a separate decision); strip grants and offer them on the capability card (needs a new card operation, and the carry above as well); drop named permission bundles (Crew's ceiling cannot see a bundle's denies, so dropping one can widen the worker); ask the agent authors for Crew-compatible specs (no outreach to agent authors is part of this decision).

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/32958 -- the on-record artifact this entry rests on: the issue Bolin Chen (bolichen97), a maintainer listed in `MAINTAINERS.md`, opened to set out this behaviour, what stays refused and the alternatives it considered.
- https://github.com/kirodotdev/KiroCrew/pull/28501 -- the pull request that added the 2026-11-02 entry this one supersedes in part.
- https://github.com/kirodotdev/KiroCrew/pull/31292 -- the open pull request whose 2026-11-06 entry keeps `subagent.trustedAgents` refused; this entry leaves out only the case a carried allow already covers.
- https://github.com/kirodotdev/KiroCrew/pull/31241 -- the open pull request that carries bare sub-agent and skill allows.
