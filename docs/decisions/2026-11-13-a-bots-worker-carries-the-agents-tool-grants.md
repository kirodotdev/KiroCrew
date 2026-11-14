# A Bot's worker carries the agent's tool grants and its partly enforceable shell denies

Decided by: Ray Xu (@buluoray, maintainer)
Date: 2026-11-13
Supersedes: 2026-11-02-a-crewmates-worker-carries-its-template-policy.md (in part: "every other `toolsSettings` key, in any form, is refused with its name" and "each only when KAS can apply every entry", for the keys named below); 2026-11-06-a-bots-worker-carries-its-templates-subagent-allowlist.md (in part: "`subagent.trustedAgents` ... and every other key outside the allowlist are still refused with their names", for `subagent.trustedAgents` and the keys named below); 2026-11-07-add-abilities-leaves-out-a-grant-the-worker-already-holds.md (in part: the option not chosen, "carry the grants KAS enforces, such as `autoAllowReadonly`, `allowedPaths` and wider `trustedAgents`")

## Decision

When a Bot adds an agent's abilities, its worker carries the agent's `shell.denyByDefault`, `shell.autoAllowReadonly`, `read.allowedPaths`, `write.allowedPaths` and `subagent.trustedAgents` (the `subagent` spelling only) exactly as an ordinary KAS session of that agent applies them, and carries `shell.deniedCommands` whole even when some of its entries cannot be enforced exactly; while such an entry is carried, a shell command that is not read-only never runs on that worker without a person's answer, whatever the Bot's permission setting, and the Bot's Abilities settings name those entries in plain words; every other part of the three entries stands, including the refusal of every other `toolsSettings` key.

## Why

- People add agents that set these keys on purpose, and the whole add was refused. The only workaround was a hand-edited copy of the agent that drops the settings and goes stale on every update.
- The KAS engine itself reads none of these keys. On an ordinary KAS session, Kiro Crew already turns `trustedAgents` and glob-shaped `deniedCommands` into permission rules the engine applies, and answers the rest at its own permission gate. A Bot's worker runs through that same projection and gate, so carrying the keys gives it what an ordinary session of the same agent gets, and nothing more.
- Every grant carried is one the agent's author wrote. Each still meets the governance ceiling at the call, the author's own restrictions, the deny rows and the sandbox. A `trustedAgents` allow is still withheld where a governance profile governs the session.
- A worker runs unattended, so a deny entry nobody can judge must not be answered automatically. Holding every shell command that is not read-only for a person is the narrowest rule that keeps the author's intent without refusing the agent.
- The 2026-11-02 entry was decided by Bolin Chen (@bolichen97) and Ray Xu; the 2026-11-06 and 2026-11-07 entries were decided by Bolin Chen. This entry changes only the parts its Supersedes line names.
- Options not chosen: keep refusing and reword the error (every such agent stays unusable on a Bot); drop the keys silently (the worker would be wider or narrower than its author declared); change the engine to read `toolsSettings` (not Kiro Crew's to change, and not needed, because the projection already carries the keys); teach Kiro Crew's command reader regex alternation so those denies are refused outright (right later, but a larger change than this one).

## Evidence

- The pull request that adds this entry, where Ray Xu restates this decision in a comment.
- https://github.com/kirodotdev/KiroCrew/issues/32958 -- the issue the 2026-11-07 entry rests on, which leaves carrying these grants to a separate decision.
- Measured on real engine sessions. With `trustedAgents` set, the named sub-agent started with no permission request, and without it the engine asked. With a glob deny, the engine blocked the command itself. With `allowedPaths`, `denyByDefault` or `autoAllowReadonly`, set or not, the engine asked every time, which leaves the answer to Kiro Crew's gate. `test/test_kas_tools_settings_grants.py` pins how that gate answers.
