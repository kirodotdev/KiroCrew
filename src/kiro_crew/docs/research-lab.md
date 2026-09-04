# Research Lab

Research Lab (the `auto-research` builtin app) runs autonomous, multi-cycle campaigns: you define a research question, a **grill tree** interactively clarifies scope and sub-questions, then the system investigates cycle-by-cycle until it satisfies a success criterion or exhausts its cycle budget. Each cycle produces a structured **finding card**, and the run yields a consolidated report (`FINDINGS.md` / HTML) exportable to the Knowledge Library.

`auto-research` is disabled by default. App enablement is the activation gate: a disabled app contributes no agents, skills, crons, or routes.

> **Research Lab is not a permission boundary.** Its `kirocrew-research` worker is
> generated from your main agent's configuration and therefore inherits the same MCP
> servers and the same tool set — it can write files, run commands, and mutate systems
> exactly as your main agent can. Research-only is the *intent* of the prompt it is
> given, not a restriction the code enforces. Review the tools your main agent has
> before leaving a campaign to run unattended. See
> [Scope and permissions](#scope-and-permissions).

## Execution modes

In the default **agent** mode, a live agent (`kirocrew-research`) drives the loop via the **autonudge** mechanism. Each cycle it reads the brief plus any pending guidance, decides the next highest-value direction from prior findings, performs one research cycle, and writes a structured finding. A Python **watchdog** polls every ~5s and deterministically enforces stagnation detection, limits, reserve-and-finalize, and success-criteria verification.

A campaign can instead use **workflow** mode, which runs the Dynamic Workflow template and translates workflow events into the same findings and report files. Workflow mode does not support in-progress guidance or adding questions after launch.

- The LLM **is** the per-cycle orchestrator; control flow is decided live.
- Supports **in-progress guidance** and **emergent sub-questions** (findings-driven
  follow-ups activated into the checklist).
- Best when the path is unpredictable and you want it to follow leads as they emerge.

## Shared domain layer

- **Grill tree** — interactive clarification that scopes the question into sub-questions
  (a well-formed starting point).
- **Success criteria** — optional natural-language done-condition, verified each cycle
  (`verification: {passed, detail}` on the finding).
- **Limits** — max cycles (safety cap), idle interval, max sub-questions per round, budget.
- **In-progress guidance** — user steering injected between cycles: the agent reads
  `guidance.txt` each cycle and incorporates it.
- **Emergent sub-questions** — findings-driven follow-ups, ranked, depth-decayed, deduped,
  activated into the checklist, ranked with decay and de-duplicated.
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
| `failed` | Unresponsive (no activity past deadline) or execution failure |
| `stopped` | User-stopped (terminal) |

Pause/resume pauses/resumes the autonudge loop.

## Scope and permissions

Research Lab writes campaign state, findings, and reports in its campaign directory. Its `kirocrew-research` worker derives from the main Kiro Crew agent configuration rather than a research-only tool allowlist. Treat Research Lab as an orchestration feature, not a permission boundary; review the tools enabled for the generated agent before running an unattended campaign.

Actions surfaced by a campaign still require the normal tool-approval flow when the configured tool requires approval.
