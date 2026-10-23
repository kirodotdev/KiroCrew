# The PR description contract

Read from SKILL.md's *PR description contract* when `.github/PULL_REQUEST_TEMPLATE.md`
is absent. With the template present, its headings are the contract and this file
adds nothing. Phase 1.5 checks the body against the diff either way.

1. **Problem / Motivation** — `**Goal:** <one sentence>`, then the symptom, or the gap for a feature.
2. **Why it matters** — impact if left unfixed. Then **Not a goal**, one bullet per scope.
3. **What changed (motivation → approach → change)** — symptom → root cause → the specific change. Three short paragraphs at most — one per arrow, well under 500 words of prose; `--check-body` stops past that (exit 21, `WORD_LIMIT`). It describes the **whole diff on this head**, never one round's fix. Write it in the register of *Writing register: Age 5* in SKILL.md.
4. **Backwards compatibility** — `Compatible:` or `Breaking:`. A diff that tightens a contract (new required field, a validator that raises on input the base accepts, narrowed type, removed kind, renamed key) cannot be `Compatible:`; it takes `Breaking:` plus a writer sweep re-run on FRESH `origin/<base>` before the final push, with that sha in the body. A diff that takes something away lists its readers as `Reader:` lines.
5. **Tests** — what was added/updated and what each locks in. A `fix:` PR adds one line under this heading, pasted from `prove.py`: `Proof: PROVEN <the tests that fail without the fix> (head <sha12>)`, or `Proof: not provable -- <reason>` with the reason from the template's closed list. Advisory for now: `--check-body` prints findings and returns exit 24 once CI enforces them.
6. **Manual verification** — steps done/needed, or "N/A — unit coverage sufficient" with a one-line why.
7. **Screenshots / video — MANDATORY for any user-visible UI change**, uploaded as GitHub attachments with `gh ... --attach`, never committed, bar one scoped exception. See *Screenshots* in SKILL.md.
8. **Issue link** — `Closes #<n>` on a line of its own. See *Issue first* in SKILL.md.
9. **Sibling sweep, then Pattern harvest** — `fix`/`revert` PRs only. `## Sibling sweep` lists every other place with the same defect, `Sibling: <path>[:<line>] -- changed here | tracked in #<n> | not affected -- <why>`, or `Siblings: none -- <why>` (advisory for now: `--check-body` prints findings, exit 23 once CI enforces it). `## Pattern harvest`: `Rule candidate:` + `Pattern:`, or `Not generalizable:`.

Omit a section only when truly not applicable, and say so.
