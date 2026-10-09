---
title: Conductor chat board -- no second full task board in a chat whose Dashboard tab already shows it
status: in-progress
author: iamwhatever, with kirocrew-worker
created: 2026-10-09
last-audited: 2026-10-09
audited-at: aee520e7da
doc-pr: null
implementation-prs: [18608]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Conductor chat board on a board tab

- Status: in-progress. The implementation is [#18608](https://github.com/kirodotdev/KiroCrew/pull/18608). The operator who runs the conductor fleet decided it on 2026-10-09 from Crew Mode user feedback. This document lands on its own first, as GOVERNANCE.md asks of an RFC, so the implementation's First Principles lane can read the decision off the base branch.
- Narrows one prompt rule on main, named in section 3: the "Talking to the person" milestone board in `_CONDUCTOR_SYSTEM_PROMPT`.
- Related: `docs/decisions/2026-10-02-conductor-introduces-itself-in-every-chat.md` (Bolin Chen's decision this keeps, section 5), [rfc-crewmate-dynamic-dashboard.md](rfc-crewmate-dynamic-dashboard.md) (the Dashboard tab and its templates).
- Measured at `aee520e7da`.

## 1. Problem

At every milestone (a task starting, finishing, getting stuck, or needing the person) the conductor draws one inline "Task board" widget in the chat: "Needs you", a done-of-total count, one row per task, and a next-step line. The rule is in the "Talking to the person" section of `_CONDUCTOR_SYSTEM_PROMPT` in `src/kiro_crew/agent.py`, added by [#16088](https://github.com/kirodotdev/KiroCrew/pull/16088).

A conductor crewmate whose Dashboard tab shows the `goal-board` template (or `work-kanban`, the same board as columns) already has that board one tab away. Both templates are folded from the work ledger with no model call (`src/kiro_crew/dashboard_templates/builtin/README.md`: "every number they show has a fold"), so the tab is always current. The in-chat copy repeats it at every milestone, and the user reported it as duplicated: the chat fills with tables they already have, and the one line that needs them is buried among them.

The conductor's turn already says which page its tab shows. `turn_block` in `src/kiro_crew/dashboard_agentic.py` emits a `[DASHBOARD]` block naming the template id (`Your Dashboard tab shows template <id> v<n>.`), injected by `src/kiro_crew/context_assembly/member.py`.

## 2. Goals and non-goals

Goals:

- With `goal-board` or `work-kanban` on the tab, the milestone reply in chat carries only the "Needs you" lines (when anything waits on the person) and one short line on what changed and what happens next.
- Everywhere else, the full in-chat "Task board" stays exactly as today.

Non-goals:

- The introduction widget rule (first reply). Unchanged.
- Any other template. A tab showing `project-report` (the default for a crewmate that adopted nothing), `session-ledger` or any page that does not draw the work items keeps the in-chat board.
- Messaging channels, scheduled runs and the CLI. They have no Dashboard tab and keep the board, as short plain text where widgets do not render.
- Changing the templates, the `[DASHBOARD]` block, or any code path. This is prompt text only.

## 3. Design

This is a generation-time rule in the conductor prompt. On a board tab the model never writes the widget, so there is nothing to hide: no client-side suppression, filter or hide is added, and the dashboard renders whatever the model writes, as today. The motivation is two costs, not one: the reader sees every task twice, and each milestone spends output tokens on a full widget plus a `task-dashboard` artifact update that the tab already shows. So on a Dashboard-tab surface the `task-dashboard` artifact is not written either.

The milestone rule branches on the `[DASHBOARD]` block:

| Tab shows | Chat reply at a milestone |
|---|---|
| `goal-board` or `work-kanban` | "Needs you" lines first, if any; then one line: what changed, what is next |
| anything else, or no tab | the full "Task board" widget, unchanged |

- **Why two template ids, not "a block exists".** Every crewmate gets a `[DASHBOARD]` block, because an unadopted crewmate falls back to `project-report` (`instance.default_instance`). That page does not draw the task board, so "a block exists" would drop the board where nothing else shows it.
- **"Needs you" stays in the chat.** It is the one part a person must act on, and the tab does not interrupt them. With nothing waiting, the line is left out rather than saying "Nothing right now".
- **The `task-dashboard` artifact.** Written on the no-tab branch only. There it is still the only full board. On a board tab it is not written: the tab is the full board, and the artifact would be a third copy paid for in output tokens.
- **Answers.** The `[OPTIONS: ...]` / `ask_question` rule moves above both branches, since both still ask the person things.

## 4. Risks

- **A stale or missing `[DASHBOARD]` block.** `turn_block` returns `""` on any read failure, so the conductor falls back to the full board. The failure mode is today's behaviour, never a missing board.
- **A person who reads only the chat.** They still get every "Needs you" item and one status line per milestone. The full board is one tab away and always current.
- **A template renamed or a new board template added.** The rule names two ids. A new board template keeps the in-chat board until the rule names it too, which errs toward today's behaviour.

## 5. The recorded decision

`docs/decisions/2026-10-02-conductor-introduces-itself-in-every-chat.md` (Bolin Chen): every conductor chat introduces itself and "reports with a live task board that puts 'Needs you' first, wherever it is opened", and refuses a Crew-Mode-only flag that would make "two different Conductors".

This change keeps it. On a board tab the live task board is the tab, and the chat still puts "Needs you" first. There is no new flag and no mode split: the same conductor reads the same `[DASHBOARD]` block on every surface, and only the page it already has decides how much it repeats. The introduction is untouched. Bolin Chen is asked to review the implementation PR.

## 6. Alternatives considered

- **Drop the in-chat board everywhere.** Rejected: surfaces with no tab (channels, cron, CLI, a crewmate on `project-report`) would lose the only board they have.
- **Key on "a `[DASHBOARD]` block exists".** Rejected: every crewmate has one through the `project-report` fallback, which shows no task board.
- **Keep the board but shrink it to a count.** Rejected: it still repeats the tab, and a count without rows answers nothing the "Needs you" line does not.
- **A user setting to hide the in-chat board.** Rejected: it is the extra flag the recorded decision refuses, and the template already tells the conductor what the reader has.
