# Architecture

How Kiro Crew fits together, one doc per cross-cutting concern. These docs are maps:
they explain structure and rationale and link out to
[../system-specs/modules/](../system-specs/README.md) for mechanism detail.

| Document | Covers |
|---|---|
| [overview.md](overview.md) | System diagrams, the component map, and the subsystem-to-spec index. Start here. |
| [mcp.md](mcp.md) | MCP server discovery, tool management, the MCP-first rule, and the tool-statelessness invariant. |
| [security-deep-dive.md](security-deep-dive.md) | The security model as a whole: threat model, trust boundaries, and how the layers compose. |
| [resource-protection.md](resource-protection.md) | Process limits, sandbox resource controls, and rate limiting. |

`design-notes/` holds narrow design records that have no owning module spec: see
[its index](design-notes/README.md).
