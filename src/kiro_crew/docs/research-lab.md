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
- **Emergent sub-questions** — findings-driven follow-ups, ranked with depth decay,
  de-duplicated, and activated into the checklist.
- **Findings + report** — per-cycle `cycle_NNN.json` cards + consolidated `FINDINGS.md`,
  exportable to the Knowledge Library or as an HTML artifact.

### File model

```
~/.kiro/crew/workspace/research/<campaign_id>/
├── brief.md               # question + sub-questions (written at launch; rewritten when
│                          #   a question is added or emergent questions activate)
├── status.json            # backend writes, agent reads each cycle
├── guidance.txt           # user nudge: agent reads + incorporates each cycle
├── emergent_questions.json # agent writes findings-driven follow-ups; ingested + consumed
├── questions.json         # agent's clarification question (or the 24h expiry prompt)
├── worker_done.json       # agent mode: the worker's own "research is done" signal
├── workflow_run.json      # workflow mode: run id + cycle offset, so a resume appends
├── grill_tree.json        # the grill tree saved at campaign creation
├── subquestion_queue.json # sub-questions pending investigation, and those already done
├── finalize.flag          # set once when the run nears its cycle budget
├── findings_for_knowledge.md # scrubbed copy exported to the Knowledge Library
├── parent_findings.md     # a fork's copy of its parent's FINDINGS.md
├── STOP                   # stop file for the worker's monitor loop
├── findings/
│   ├── cycle_000.json      # { cycle, summary, key_insight, sources_checked, sources_empty,
│   ├── cycle_001.json      #   new_findings_count, evidence_strength, verification }
│   └── ...
└── FINDINGS.md            # cumulative report
```

The agent writes each `cycle_NNN.json` finding card as it completes a cycle,
beginning with `cycle_000.json`.

## Campaign lifecycle

| Status | Meaning |
|--------|---------|
| `ready` | Created, not started |
| `running` | Loop active, cycling |
| `paused` | User-paused or loop temporarily stopped |
| `stagnant` | 5 consecutive cycles with zero new findings |
| `needs_input` | Agent asked a clarification question (attended mode), or the 24h tool auto-approval expired |
| `complete` | Success criteria met OR max_cycles reached |
| `failed` | Unresponsive (no activity past deadline) or execution failure |
| `stopped` | You pressed Stop, or the worker ended the run itself |

`complete` and `stopped` are not final: you can resume either one, or fork it.

In agent mode, pause/resume pauses/resumes the autonudge loop. In workflow mode, pause
cancels the workflow run and resume starts a new one that appends to the same findings.

### Who changes a campaign

- **You**, from the dashboard: create, start, pause, resume, stop, fork, add a question,
  nudge with guidance, export, delete.
- **The watchdog**, on its own: it moves a running campaign to `complete`, `stagnant`,
  `needs_input`, `stopped` (the worker ended the run itself) or `failed`. After 24 hours it
  withdraws the worker's tool auto-approval and waits in `needs_input`. Either **Resume**
  or a **Nudge** with guidance restarts it and grants a fresh 24 hours of auto-approval.
  While the app is disabled it pauses every research loop and withdraws that approval.
- **One status change at a time.** Your status actions and the watchdog's decisions on the
  same campaign wait their turn, and a decision the watchdog reached about a run you have
  since paused and resumed is discarded, never applied to the new run.
- **The worker** never changes status itself. In agent mode it writes findings,
  `FINDINGS.md`, `emergent_questions.json`, `questions.json` and, when it decides the research
  is done, `worker_done.json` before stopping its loop; the watchdog reads those files and
  decides. In workflow mode the watchdog reads the workflow run instead.

## Scope and permissions

Research Lab writes campaign state, findings, and reports in its campaign directory. Its `kirocrew-research` worker derives from the main Kiro Crew agent configuration rather than a research-only tool allowlist. Treat Research Lab as an orchestration feature, not a permission boundary; review the tools enabled for the generated agent before running an unattended campaign.

Actions surfaced by a campaign still require the normal tool-approval flow when the configured tool requires approval.
