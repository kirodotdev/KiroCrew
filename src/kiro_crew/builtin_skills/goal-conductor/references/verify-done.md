# Patrol step 3: verifying `done`

Moved verbatim from the goal-conductor `SKILL.md` core; the core says when to read each part.

   `accept_eval` tool with NO arguments, or with `item_id=<id>` for the one item
   a wake named. It reads the bar of each item whose status is `done` from your
   ledger and returns the evaluator's own `{"results": [...]}` document, one
   `{id, verdict, evidence}` per item, with any `done` item it cannot judge
   under `skipped` and the reason. Each answer carries `reported`, the worker's
   own `summary` and claimed `pr`: a claim to read, never the bar; `summary` is
   up to 500 characters of worker prose, so decide from `verdict`, `status` and
   the evaluator (the other fields: `references/ledger-fields.md`).
   You need no `work_ledger_read` first and you
   copy no document. It runs nothing you name and does not prompt.

   **It evaluates `done` items only, and that is a rule, not a convenience.**
   The evaluator
   answers a world-state question ("does this file exist", "are this PR's checks
   green"), and a worker that is still `progress` can have made that true early:
   a stub written before the real content, a PR that is green before the last
   commit. Evaluating that item returns a genuine `pass` on unfinished work, and
   recording it then closes the item under
   the worker. A `done` is the worker saying the world-state now means what the
   condition says; only then is the evaluator's answer an acceptance.

   **Never hand the evaluator an unfiltered `accept_batch`.** To hand it a
   document yourself, or without the `accept_eval` tool (a quoted heredoc
   only, never `printf` or `echo`), read
   `<this skill's dir>/references/accept-eval-fallback.md` first and follow it exactly.

   Evaluate **every `done` item in ONE call** — a shell fallback call costs one
   approval prompt — then, in a later step once it has returned, settle each
   answer. A `pass` takes ONE write: `work_ledger_record` `action=close`
   `state=accepted` `verdict=pass`, which records the verdict and then closes
   the item. Any other answer is recorded with `action=verdict` (with `fails` when you are
   counting retries).

   Verdicts: `pass` / `fail` are final for this cycle. `pending` means keep
   waiting, within the budget below. `refused` means the spec asked for
   something the evaluator will not do — most often naming a command, which it
   does not accept from a spec at all. Re-express the condition as `pr_checks`;
   never try to route around a
   refusal. `error` is a broken spec or environment — fix the spec.

   **Two-phase acceptance: the server omits the item, you promote the value.** A
   condition may name a value that only exists after the item starts — a PR number for
   `pr_checks` is the common case. Store the condition with the value marked TBD
   at `create`, tell the child in its seed to report the number through
   `work_report`'s `pr`. Until you promote the real value the item is absent from
   `accept_batch` (`acceptance_concrete: false`). **The worker's claimed `pr` is
   never read as the bar** (your prompt's claimed-PR rule): promote it yourself
   with `action=accept` once you have looked at it, and verify on the next
   cycle. Promoting a bar clears the item's verdict, so the new bar needs its own
   `pass` before the item can close `accepted`. For a PR the goal wants merged
   the bar is `pr_merged`, as your prompt's rule on landed code says.

   **A reviewer grills your plan and what no lane reviews**, as your prompt's
   plan-reviewer rule says. Its seed carries your plan (items, acceptance,
   assumptions, what the last round produced). A `pr_*` item in a repo with
   review CI lanes needs no review here, since those lanes are part of its
   checks. It reports in ≤500 chars with paths in `artifacts` and the full list
   in a file it names; a fix it finds becomes a new item only when a Done when
   condition needs it (otherwise park it). The verdict stays with `accept_eval`.
