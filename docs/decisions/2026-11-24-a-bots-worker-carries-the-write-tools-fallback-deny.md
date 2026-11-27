# A Bot's worker carries the write tool's fallback deny

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-24
Supersedes: 2026-11-13-a-bots-worker-carries-the-agents-tool-grants.md (in part: "including the refusal of every other `toolsSettings` key", for the write tool's `fallbackAction` only); 2026-11-02-a-crewmates-worker-carries-its-template-policy.md (in part: "every other `toolsSettings` key, in any form, is refused with its name", for the write tool's `fallbackAction` only); 2026-11-06-a-bots-worker-carries-its-templates-subagent-allowlist.md (in part: "every other key outside the allowlist are still refused with their names", for the write tool's `fallbackAction` only)

## Decision

When a Bot adds an agent's abilities, its worker also carries the agent's `toolsSettings.write.fallbackAction`, under any write spelling, when its value is a deny (`"deny"` in any case, or a value that is not a string, as Crew's gate reads it) or the default, which changes nothing; with a deny, a write-tool call that none of the agent's `allowedPaths` covers is refused without asking, exactly as on an ordinary KAS session of that agent. Every other value, `fallbackAction` on any other tool, and every other `toolsSettings` key those entries refuse are still refused by name.

## Why

- A person wants a Bot that writes only inside one folder and is refused everywhere else. Without the key, a write outside `allowedPaths` asks for approval, and an unattended heartbeat has nobody to answer it, so the heartbeat stays off.
- On an ordinary KAS session Crew's own gate already enforces a deny value. A Bot's worker runs through that same projection and gate, which is the 2026-11-13 entry's test for carrying a key.
- A value that would let a write through unasked, such as `"allow"`, is not applied on KAS, so it stays refused.
- The upgrade move follows the 2026-11-14 entry: an old agent's write entry with this key is now one its worker applies exactly, so a Bot moved from then on gets it in its file instead of a hold, and behaves as the old agent did.
- The 2026-11-13 entry was decided by Ray Xu, the 2026-11-02 entry by Bolin Chen (@bolichen97) and Ray Xu, and the 2026-11-06 entry by Bolin Chen. This entry changes only the parts its Supersedes line names.
- Options not chosen: keep refusing (the agent stays unusable on a Bot); carry every value (an `"allow"` would let an unattended worker write outside its paths, and KAS does not apply it); refuse the default value too (a refusal of a setting that has no effect).

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/41941 -- the request.
- https://github.com/kirodotdev/KiroCrew/issues/41941#issuecomment-7141234688 -- the maintainer's decision on that issue.
