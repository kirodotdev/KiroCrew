# Triage Labels

Maps Matt Pocock's canonical triage roles to the labels this repo's factory
(Archon's SDLC pack) reads. Skills such as `/to-tickets` and `/triage` use the
right-hand column.

| Label in mattpocock/skills | Label in our tracker | Meaning |
| --- | --- | --- |
| `needs-triage` | `archon-triage` | Ask the factory's intake to triage this issue |
| `needs-info` | `needs-info` | Waiting on the reporter |
| `ready-for-agent` | `archon-triage` | Fully specified; intake routes it to the build queue |
| `ready-for-human` | `ready-for-human` | Needs human implementation; the factory leaves it alone |
| `wontfix` | `wontfix` | Will not be actioned |

Why `ready-for-agent` maps to `archon-triage` and not `archon-ready`: intake
creates the work item and decides the route (`archon-ready`, `archon-plan` or
`archon-investigate`). A ticket that skips intake has no work item, so the build
queue would not pick it up.
