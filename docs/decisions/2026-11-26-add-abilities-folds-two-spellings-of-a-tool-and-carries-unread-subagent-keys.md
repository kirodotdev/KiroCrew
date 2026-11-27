# Add abilities folds two spellings of one tool into the stricter entry, and carries sub-agent keys no engine reads

Decided by: Ray Xu (@buluoray, maintainer)
Date: 2026-11-26
Supersedes: 2026-11-02-a-crewmates-worker-carries-its-template-policy.md (in part: "a field that cannot be carried exactly onto the worker ... refused with an error naming the field", for a tool written under two kiro-cli spellings, and "every other `toolsSettings` key, in any form, is refused with its name", for `subagent.autoAllow` and `subagent.defaultAgent`); 2026-11-06-a-bots-worker-carries-its-templates-subagent-allowlist.md (in part: "every other key outside the allowlist are still refused with their names", for the same two keys and the `use_subagent` spelling when it is folded with `subagent`); 2026-11-13-a-bots-worker-carries-the-agents-tool-grants.md (in part: "including the refusal of every other `toolsSettings` key", for the same two keys)

## Decision

When a Bot adds an agent's abilities and the agent sets one tool under two or more kiro-cli spellings of one alias group (such as `shell` and `execute_bash`, or `write` and `fs_write`), the add folds them into one entry under the spelling kiro-cli reads first, keeping the stricter value of each key: every entry either spelling denies, only the grants every spelling makes, the narrower `availableAgents`, `denyByDefault` when either sets it, and `autoAllowReadonly` only when every spelling sets it; the preview names each fold, and the add is refused, naming the key, only when a key cannot be combined that way. `subagent.autoAllow` and `subagent.defaultAgent` are carried onto the worker as written. Every other part of the three entries stands.

## Why

- Agents written for kiro-cli often set one tool under two spellings, or set these sub-agent keys, and the whole add was refused. Owners had no way forward short of a hand-edited copy of the agent.
- kiro-cli reads only the first spelling of a group, so carrying both could not be exact. The fold never allows a call that either spelling alone would not, so the worker is never wider than the agent; it can only be narrower, and it asks where it would have asked.
- Neither KAS nor kiro-cli's documented sub-agent settings read `autoAllow` or `defaultAgent`. An ordinary session of the agent ignores them, so carrying them as written keeps the worker the same as that session.
- Options not chosen: keep refusing with a better message (every such agent stays unusable on a Bot); keep only the spelling kiro-cli reads (the denies under the other spelling would be lost); drop the two sub-agent keys with a notice (adds a row kind for keys that change nothing).

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/43357#issuecomment-7209201645 -- Ray Xu's statement of this decision.
- https://github.com/kirodotdev/KiroCrew/issues/43356 -- Add abilities refuses an agent that sets one tool under two spellings.
- https://github.com/kirodotdev/KiroCrew/pull/43358 -- the pull request that implements it.
