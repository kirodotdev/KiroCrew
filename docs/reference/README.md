# Reference

A mirror of upstream documentation, kept for offline use. **Default
rule: do not author these files.** Fix an error upstream first — a silent local
edit diverges from the source the page claims to mirror.

Named exceptions are allowed, and they are what keeps the rule honest. A page that
is not a mirror, or that carries local additions on top of mirrored content, says
so **in its own body** and is marked in the mirror's Contents table. Anything not
marked is a plain mirror and a re-fetch may overwrite it wholesale; anything marked
must have its local content preserved across a re-fetch. Adding an exception means
writing both marks, not editing the page and leaving the rule to look broken.

| Mirror | Upstream |
|---|---|
| [kiro-cli/](kiro-cli/README.md) | The `kiro-cli` documentation, mirrored from the current 3.0 information architecture, with version-specific measurements called out in place. kiro-cli is Kiro Crew's default agent backend, so its ACP surface, agent-spec schema, and MCP config shape are load-bearing here. |
| [ledger-conductor-sequence.md](ledger-conductor-sequence.md) | **Named exception — local page, no upstream.** Kiro Crew's own end-to-end sequence for the work-ledger dispatch lifecycle. Preserve it across a re-fetch. |
| [crew-log/](crew-log/README.md) | **Named exception — local pages, no upstream.** API-style reference for the append-only crew log: envelope, session and crew entry types, member-log linkage, the reader and writer surface, and the error codes. Preserve it across a re-fetch. |

Where Kiro Crew's own behavior differs from a mirrored page, Kiro Crew's docs win.
These pages document the `kiro-cli` backend; Kiro Crew also supports other ACP
backends, whose contracts live in the [providers specification](../system-specs/modules/providers.md).
A capability shown in this mirror is not automatically a Kiro Crew capability.
