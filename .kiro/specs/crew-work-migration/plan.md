# plan.md — linkage

**Status:** draft

<!--
  REVIEWER: change the line above to `**Status:** accepted` to open the SDLC
  test/deploy gate. The agent deliberately left it `draft`: it wrote the plan
  this file points at, and an agent may not approve its own work.

  Check the gate deterministically:
    python3 ~/.kiro/crew/skills/ai-native-sdlc/scripts/sdlc_gate.py \
      .kiro/specs/crew-work-migration deploy
-->

This is a **thin linkage file**, not a document — see [`spec.md`](spec.md) for why
both exist.

**Source of truth:** [`tasks.md`](tasks.md), the implementation plan with its
per-task requirement references and completion state.

Current state at the time of writing: tasks 0–4 complete, task 5 partial (5.2's
Req 7.3 done, Req 7.2 blocked on a transmit step). Evidence and the honest limits
are in [`REVIEW.md`](REVIEW.md) §4 and §6 — read §6 before accepting this plan as
done, because "planned" and "wired in production" differ here and the difference
is deliberate.
