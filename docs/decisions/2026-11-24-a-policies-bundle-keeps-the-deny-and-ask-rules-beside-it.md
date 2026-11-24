# A permissions block that names a policies bundle keeps its deny and ask rules and leaves the bundle and its allow rules out

Decided by: Ray Xu (buluoray), Kiro Crew maintainer
Date: 2026-11-24

## Decision

When an agent's `permissions` block names a `policies` bundle, Crew leaves out only the bundle and the `allow` rules beside it, and the block's `deny` and `ask` rules reach KAS as written and decide which built-ins stay at the prompt; the `allow` rules stay refused until a separate decision.

## Why

- The 2026-11-15 call on the issue kept such a block refused whole. Ray Xu replaced it for `deny` and `ask` rules only.
- A `deny` or `ask` only narrows, so carrying one grants nothing a bundle meant to hold back.
- Refusing the block whole lost an author's MCP `deny`: KAS then received only the `allowedTools` allow for that server, so the denied call ran.
- With the explicit rules read, a shell, read or write `deny` beside a bundle is refused by KAS without a prompt, and a capability the author did not restrict is no longer held at the prompt.
- The session-start notice says: "Allow rules beside a policies bundle aren't used, so those tools ask first. To skip the ask, write the rules out without the bundle, or list the tools in allowedTools." The Add abilities refusal says `permissions (allow rules beside a policies bundle; write them out without the bundle, or use allowedTools)`.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/34368#issuecomment-7141413356 -- Ray Xu's decision comment.
- https://github.com/kirodotdev/KiroCrew/issues/34368 -- the report this decision answers.
