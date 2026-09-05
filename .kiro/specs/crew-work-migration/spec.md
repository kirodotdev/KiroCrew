# spec.md — linkage

**Status:** draft

<!--
  REVIEWER: change the line above to `**Status:** signed-off` to open the SDLC
  build/test gate. The agent deliberately left it `draft`: it wrote the artifacts
  this file points at, and an agent may not approve its own work.

  Check the gate deterministically:
    python3 ~/.kiro/crew/skills/ai-native-sdlc/scripts/sdlc_gate.py \
      .kiro/specs/crew-work-migration build
-->

This is a **thin linkage file**, not a document. It exists because two naming
conventions meet in this directory:

| SDLC stage artifact | Actual file(s) in this Kiro spec |
| --- | --- |
| `spec.md` (Design) | [`requirements.md`](requirements.md) — EARS acceptance criteria<br>[`design.md`](design.md) — the design |
| `plan.md` (Build) | [`tasks.md`](tasks.md) |

`sdlc_gate.py` looks for `spec.md` and `plan.md`; Kiro's spec tooling reads
`requirements.md`, `design.md` and `tasks.md`. Renaming the Kiro files to satisfy
the gate would break the tooling that actually consumes them, and duplicating
their content here would create two sources of truth that drift. The skill's own
brownfield guidance calls for exactly this: name one source of truth per
artifact, with a thin linkage where the names differ.

**Source of truth:** `requirements.md` + `design.md`. This file carries only the
gate status.
