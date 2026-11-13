# A crewmate's worker carries its template's hooks, permissions and tool settings; the conductor is unchanged

Decided by: @bolichen97 (maintainer) and Ray (@buluoray, repo admin), both on the pull request on 2026-11-03
Date: 2026-11-02

## Decision

Adding an agent's abilities to a crewmate copies that agent's `hooks`, `permissions` rules (deny, ask and allow) and an allowlist of `toolsSettings` keys into the crewmate's file exactly as the agent declares them, and the crewmate's worker runs under them on KAS, never wider than the agent declares; the `toolsSettings` allowlist is `deniedCommands` and `allowedCommands` on the shell tool, `deniedPaths` on the read and write tools, and `web_fetch.blocked`, each only when KAS can apply every entry, and every other `toolsSettings` key, in any form, is refused with its name; the crewmate's chat (the conductor) keeps running the stock conductor spec and takes nothing from the file; a field that cannot be carried exactly onto the worker, and a crewmate whose worker runs on a backend that applies none of these fields (any backend but KAS), are refused with an error naming the field.

## Why

- Since the conductor and worker split, a template that set permission rules, tool settings or hooks was refused for a crewmate, and crewmates the upgrade moved off such a template could not get its tools back.
- The worker is where the template's tools run, so it is where the template's rules apply; the conductor stays as the split left it, with nothing from the crewmate's file reaching it, as `docs/request-for-change/rfc-mate-owned-agent-config.md` §5.1 says.
- This entry does not change the upgrade migration recorded in `2026-10-27-every-used-crewmate-moves-to-the-conductor.md` and `2026-10-28-every-crewmate-on-a-user-level-agent-moves-to-the-conductor.md`: the file the migration writes still carries no hooks, permissions or tool settings, and its notice still names them; adding the template's abilities again now carries them onto the worker.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/26881 -- crewmate tool restrictions are not carried; closed by the pull request below.
- https://github.com/kirodotdev/KiroCrew/pull/23010 -- the conductor and worker split this follows.
- https://github.com/kirodotdev/KiroCrew/pull/28501#issuecomment-6595732529 -- the maintainer's statement of this decision.
- https://github.com/kirodotdev/KiroCrew/pull/28501#issuecomment-6595520476 -- the repo owner's statement of this decision.
- https://github.com/kirodotdev/KiroCrew/pull/28501 -- the pull request that adds this entry and implements it.
