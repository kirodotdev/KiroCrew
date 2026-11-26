# Work items: acceptance kinds and a worked example

Moved verbatim from the goal-conductor `SKILL.md` core; the core says when to read each part.

## Assertable

2. **Assertable** — you can name its completion condition *now*, before
   dispatching, as one of the evaluator's kinds: `pr_checks` (a non-draft
   PR's checks all green), `pr_merged`, `edition_check`, `git_commit` (a
   local branch has a commit past a base sha), `file` (a path existing) or
   `human_approval` (the user accepts it). For `pr_checks` and
   `pr_merged`, always set `repo` to `owner/name`, with `repo` required.
   **There is deliberately no "run this command" kind**, so "the test suite
   passes" is expressed as `pr_checks` on the PR that carries the work — CI
   runs the suite, and its verdict is the one that counts. If an item's
   completion genuinely cannot be stated as one of these, it is not
   assertable: make it a `file` item (a report the worker writes), or
   `human_approval` only when a person must judge the result; never invent a
   condition. Before you write a kind you have not used in this goal, read
   `<this skill's dir>/references/acceptance-kinds.md` (each kind's object and verdicts; how a
   `human_approval` item is asked and settled is in `references/rare-cases.md`).

## Worked example

Worked example — goal "resolve this repo's open issues":

| Candidate | Verdict |
|---|---|
| Triage the open issues | Yours. One API read plus a classification; not worth a session's context. |
| Queue the actionable ones | Not a task. It is triage's completion condition. |
| Fix issue #N, label it | **Work item** — one per issue; issues meant for one PR are one item. |
| Check status and pick the next round | Yours. This is the control loop. |
| Advise the user on issues nobody can action | Not a task. Run the needs-human checklist (Stop conditions). |
| Write the summary report | Yours. A fold over the ledger. |

## Weak and strong items

A strong item names one outcome a check can see, and its acceptance is that
check. A weak one names an activity, so no verdict can tell done from busy.

| Weak item | Why it is weak | Strong item |
|---|---|---|
| "Improve the CLI docs", `human_approval` | no outcome a check can see; the person ends up grading effort | "Document the `--json` flag in the CLI guide", `pr_merged` on the PR that adds it |
| "Look into the flaky test", `file` at a notes path | the file proves a file was written, not that anything was found | "Name the root cause of the flaky test with the failing log line and its `path:line`", `file` at that path, the seed saying what the file must hold |
| "Fix #12 and #15, and clean up the parser" | three lines of work behind one verdict, and the clean-up is a finding no Done when condition asked for | one item per issue, each `pr_merged`; the clean-up goes to Parked |
