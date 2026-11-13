# Architecture

How Kiro Crew fits together, one doc per cross-cutting concern. These docs are maps:
they explain structure and rationale and link out to
[../system-specs/modules/](../system-specs/README.md) for mechanism detail.

| Document | Covers |
|---|---|
| [overview.md](overview.md) | System diagrams, the component map, and the subsystem-to-spec index. Start here. |
| [mcp.md](mcp.md) | MCP server discovery, tool management, the MCP-first rule, and the tool-statelessness invariant. |
| [agent-mcp-and-skill-reach.md](agent-mcp-and-skill-reach.md) | Which MCP servers and skills the Ultra Mode and crewmate conductors and workers mount, and whether a newly installed server or skill reaches each. |
| [dashboard-iframe-hosts.md](dashboard-iframe-hosts.md) | The four dashboard iframe hosts, their differing sandboxes, why they are not interchangeable, and how to add a panel tab kind. |
| [app-platform-trust-model.md](app-platform-trust-model.md) | Why an enabled app runs in-process with the gateway's privileges, the confinement that does apply, and what each layer is worth. |
| [followup-suggestions-trust-model.md](followup-suggestions-trust-model.md) | The gates behind the follow-up card: argument validation, the branch-name filter, and the sandboxed `git` invocation. |
| [context-management.md](context-management.md) | What reaches the model's context: the first-turn block order and its budgets, what repeats per turn, a sub-agent's scope, why a custom agent sees no skills, and how a crew member's session is built. |
| [security-deep-dive.md](security-deep-dive.md) | The security model as a whole: threat model, trust boundaries, and how the layers compose. |
| [resource-protection.md](resource-protection.md) | Process limits, sandbox resource controls, and rate limiting. |
| [harness-requirements.md](harness-requirements.md) | The checklist of what every harness must do, each row with the PRs that taught it, its edge cases, and the test that checks it. |
| [harness-gap-analysis.md](harness-gap-analysis.md) | Where each harness stands against that checklist (met, not met, unknown), and a ranked list of the fixes that close the most gaps. |

`design-notes/` holds narrow design records that have no owning module spec: see
[its index](design-notes/README.md).
