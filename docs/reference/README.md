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
| [kiro-cli/](kiro-cli/README.md) | The `kiro-cli` documentation, taken from the 2.x line. kiro-cli is Kiro Crew's required agent backend, so its ACP surface, agent-spec schema, and MCP config shape are load-bearing here. |

Where Kiro Crew's own behavior differs from a mirrored page, Kiro Crew's docs win:
this fork is KiroACP-only and does not use every capability the upstream CLI
documents.
