# Research Lab

Research Lab (the `auto-research` builtin app) runs autonomous, multi-cycle campaigns:
you define a research question, a **grill tree** interactively clarifies scope and
sub-questions, then the system investigates cycle-by-cycle until it satisfies a success
criterion or exhausts its cycle budget. Each cycle produces a structured **finding card**,
and the run yields a consolidated report (`FINDINGS.md` / HTML) exportable to the Knowledge
Library.

> **Research Lab is research-only by design.** It gathers, analyzes, and reports — it does
> **not** take actions on your systems (no writes, deployments, mutations, or code changes).
> Its sub-agents run with read/research tools only, and any follow-up action is handed off
> to the main agent for you to review and drive. This keeps campaigns safe to run
> unattended and their results reproducible. See [Scope](#scope-research-only-by-design).

## Execution: Agent (adaptive)

A live agent (`kirocrew-research`) drives the loop via the **autonudge** mechanism. Each
cycle it reads the brief plus any pending guidance, decides the next highest-value
direction from prior findings, performs one research cycle, and writes a structured
finding. A Python **watchdog** polls every ~5s and deterministically enforces stagnation
detection, limits, reserve-and-finalize, and success-criteria verification.

- The LLM **is** the per-cycle orchestrator; control flow is decided live.
- Supports **in-progress guidance** and **emergent sub-questions** (findings-driven
  follow-ups activated into the checklist).
- Best when the path is unpredictable and you want it to follow leads as they emerge.

## Shared domain layer

- **Grill tree** — interactive clarification that scopes the question into sub-questions
  (a well-formed starting point). See `GrillTree.tsx`.
- **Success criteria** — optional natural-language done-condition, verified each cycle
  (`verification: {passed, detail}` on the finding).
- **Limits** — max cycles (safety cap), idle interval, max sub-questions per round, budget.
- **In-progress guidance** — user steering injected between cycles: the agent reads
  `guidance.txt` each cycle and incorporates it.
- **Emergent sub-questions** — findings-driven follow-ups, ranked, depth-decayed, deduped,
  activated into the checklist (`subquestion_queue.py`).
- **Findings + report** — per-cycle `cycle_NNN.json` cards + consolidated `FINDINGS.md`,
  exportable to the Knowledge Library or as an HTML artifact.

### File model

```
~/.kiro/crew/workspace/research/<campaign_id>/
├── brief.md               # question + sub-questions (written at launch)
├── status.json            # backend writes, agent reads each cycle
├── guidance.txt           # user nudge: agent reads + incorporates each cycle
├── emergent_questions.json # agent writes findings-driven follow-ups; ingested + consumed
├── findings/
│   ├── cycle_001.json      # { cycle, summary, key_insight, sources_checked,
│   ├── cycle_002.json      #   sources_empty, evidence_strength, verification }
│   └── ...
└── FINDINGS.md            # cumulative report
```

The agent writes each `cycle_NNN.json` finding card as it completes a cycle.

## Campaign lifecycle

| Status | Meaning |
|--------|---------|
| `ready` | Created, not started |
| `running` | Loop active, cycling |
| `paused` | User-paused or loop temporarily stopped |
| `stagnant` | 5 consecutive cycles with zero new findings |
| `needs_input` | Agent asked a clarification question (attended mode) |
| `complete` | Success criteria met OR max_cycles reached |
| `failed` | Unresponsive (no activity past deadline) |
| `stopped` | User-stopped (terminal) |

Pause/resume pauses/resumes the autonudge loop.

## Key files

| File | Role |
|------|------|
| `apps/builtins/auto_research/handlers.py` | Campaign CRUD, watchdog, grill tree, agent launch, emergent-exploration, findings, reports, SSE |
| `apps/builtins/auto_research/subquestion_queue.py` | Emergent sub-question queue (ranking, decay, dedup) |
| `website/src/apps/auto-research/ResearchLabPage.tsx` | Setup wizard, campaign list/detail, finding cards |
| `website/src/apps/auto-research/GrillTree.tsx` | Grill-tree clarification UI |

## Scope: research-only by design

Research Lab **investigates and reports; it does not act.** This is a deliberate product
boundary, not a missing feature:

- **No actions.** Campaigns never write, deploy, mutate state, or change code. They read,
  analyze, and produce findings + a report.
- **Sub-agents are tool-scoped to research.** RL's research sub-agents run with
  read/research tooling only. They cannot be used to take actions, so an unattended campaign
  has a small, well-understood blast radius.
- **Actions are deferred to the main agent.** When a campaign surfaces a next step that
  requires acting (e.g. "apply this migration"), that hand-off goes to the main agent, where
  you review and drive it with the normal tool-approval flow — rather than RL performing it
  autonomously mid-campaign.

**Why research-only.** Keeping RL to research keeps it honest about what it is: an adaptive
but bounded investigation engine. Folding autonomous, adaptive *action-taking* into RL would
blur that identity (a research engine that also mutates systems is neither cleanly
reproducible nor cleanly safe) and widen the sub-agent capability surface. If you need
adaptive action-taking today, drive it from the main agent (which already has approval gates
and full-tool judgment).
