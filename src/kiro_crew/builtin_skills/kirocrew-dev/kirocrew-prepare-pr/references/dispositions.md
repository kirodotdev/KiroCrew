# Writing and recording dispositions

Moved verbatim from the kirocrew-prepare-pr `SKILL.md` core.

## What must be answered

**What must be answered:**

- A **BLOCK** from the **whole-design lanes** — Design, First Principles, UX: every Blocker, each with its own `target=design` / `target=first-principles` / `target=ux` comment naming its `span=`. It blocks readiness. `pr_findings.py` prints each item with its `span=`, `Trigger:`, `Outcome:` and `Clears when:` line, above the line-level findings. A PASS, and every `Notes:` bullet, needs no reply.
- One-way-door concerns from Design Review — fix or justify in writing.
- Human review comments and inline threads.

**Whole-design lanes outrank line-level lanes in triage order.** Every round,
read the Design / First Principles / UX verdicts first, decide the shape the round
ends with, then triage GPT/Opus findings against that shape.

**Per concern, individually.** Never one blanket line for a batch. Reply in the
thread when it is a thread, as a PR comment when it is a top-level bot verdict, and
resolve what you addressed.

## Write the disposition for the ledger, not for a reader

The remote GPT lane's **ADJUDICATION LEDGER** keeps only the marker,
lines beginning `> `, and `- **title**` bullets: **twelve lines at most** per
comment. Put rationale in `> ` lines; other prose is for humans only.

- Rebut the finding's class with code evidence, not just its current location;
  a recorded tradeoff applies wherever that same class moves, not to new defects.
- For a security-class rebuttal, argue *not a defect*, not *disproportional*.
  Security/data-loss/corruption cannot converge by deferral or by accepting a
  real defect as too costly.

## Recording dispositions (Phase 3 step 3)

3. **Record dispositions.** For each fixed/rebutted GPT finding, post one comment
   beginning `<!-- ai-review-disposition target=gpt head=<prior-reviewed-sha> -->`.
   The `head=` scopes the ruling to the commit it judged, not the new fix SHA.
   Name its exact printed `span=<id>` on the marker or a `- **...**` title bullet,
   never in quoted evidence. Include outcome and rationale in `> ` lines, the outcome
   first (`> fixed in <sha>: ...`, `> Rebutted: ...`): `--rounds` reads only that line.
   **One comment covers exactly one lane, and one rationale covers exactly one finding.**
   Design, UX and First Principles use their own `target=` and one comment per item.

   `pr_status.py` and the server's `--disposition-gate` reject multiple spans,
   multiple finding-title bullets, cross-lane or nonexistent spans, or no span
   when that lane has live findings. Edit or delete an invalid comment, not code;
   the readiness self-heal sweep picks it up within about 15 minutes, or run
   `gh workflow run pr-readiness.yml -f pr=<n> -f sha=<head>`.

   A disposition can downgrade repeats but never waives a new defect. Do not instruct
   the next reviewer, and never treat it as the current-SHA-scoped human override.
   Optional `self-added: yes`, `mechanism: <one line>` and
   `resolution: removed|narrowed|deferred ...` lines go right after the marker, OUTSIDE `> `.
