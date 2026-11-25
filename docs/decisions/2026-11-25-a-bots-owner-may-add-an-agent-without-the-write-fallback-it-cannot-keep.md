# A bot's owner may add an agent without the write fallback it cannot keep

Decided by: Ray Xu (maintainer, @buluoray)
Date: 2026-11-25
Supersedes: 2026-11-24-a-bots-worker-carries-the-write-tools-fallback-deny.md (in part: "Every other value ... still refused by name", for the write tool's `fallbackAction` only, when the owner chooses to add without it); 2026-11-02-a-crewmates-worker-carries-its-template-policy.md (in part: "a field that cannot be carried exactly onto the worker ... are refused with an error naming the field", for the write tool's `fallbackAction` only, when the owner chooses to add without it); 2026-11-13-a-bots-worker-carries-the-agents-tool-grants.md (in part: the option not chosen "drop the keys silently", for the write tool's `fallbackAction` only, and only as the owner's explicit choice, never silently)

## Decision

When a bot cannot keep an agent's write `fallbackAction` (any value but a deny or the default, under any write spelling), Add abilities and Enroll as bot still refuse the add by name, and the refusal offers **Add without it**. When the owner picks it, the add leaves that one value out of the bot's file and keeps everything else; a write that none of the agent's `allowedPaths` covers then goes to the bot's approval mode (it asks unless that mode skips asking) instead of running unasked. The agent's own file is never changed. The bot never keeps a non-deny value, and never drops it without the owner's choice. Every other setting the bot cannot keep is still refused by name.

## Why

- No dashboard screen edits an agent's `toolsSettings`, so the refusal alone left the owner with no way forward except hand-editing the agent's JSON file.
- Leaving the value out only narrows what the agent's author wrote: writes outside `allowedPaths` stop running unasked.
- Options not chosen: set the bot to deny (much stricter than the author's `"allow"`); rewrite the user's agent file (changes the agent's ordinary sessions too, and is someone else's file); drop the value silently (the owner would not know the bot differs from the agent).
- The 2026-11-24 entry was decided by Bolin Chen, the 2026-11-02 entry by Bolin Chen and Ray Xu, and the 2026-11-13 entry by Ray Xu. This entry changes only the parts its Supersedes line names.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/42732#issuecomment-7171380586 -- the maintainer's decision.
- https://github.com/kirodotdev/KiroCrew/issues/41941 -- the request.
