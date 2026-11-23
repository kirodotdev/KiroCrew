# Work items: acceptance kinds and a worked example

Moved verbatim from the goal-conductor `SKILL.md` core; the core says when to read each part.

## Assertable

2. **Assertable** — you can name its completion condition *now*, before
   dispatching, as one of the evaluator's kinds: `pr_checks` (a non-draft
   PR's checks all green), `pr_merged`, `edition_check`, `file` (a path
   existing) or `human_approval` (the user accepts it). For `pr_checks` and
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
