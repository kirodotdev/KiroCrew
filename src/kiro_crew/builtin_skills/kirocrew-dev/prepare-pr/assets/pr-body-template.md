## Problem
<What is broken or missing — the concrete symptom, ideally what the user observes.>

## Why it matters
<Impact on users if left unfixed: who is affected and how badly.>

## Fix (symptoms → root cause → change)
<Chain of thought: start from the observed symptom, trace to the underlying
root cause, then to the specific change that addresses that cause. The reader
should follow *why this change is the right one*, not just what changed.>

## Tests
<Automated tests added/updated and the behavior each one locks in.>

## Manual verification
<Manual steps performed or still required where unit tests are not enough
(integration paths, UI, external services). State "N/A — unit coverage
sufficient" only when genuinely true, with a one-line why.>

## Screenshots
<MANDATORY for user-visible UI changes; delete this section otherwise.
Commit images to the PR branch under temp-screenshots/<feature>/ (a top-level,
ephemeral, never-packaged dir — never under docs/ or src/kiro_crew/**) and embed
with commit-SHA-pinned URLs so they survive branch deletion on merge and periodic cleanup:
![alt](https://github.com/<owner>/<repo>/raw/<sha>/temp-screenshots/<feature>/<name>.png)
Show each affected surface's meaningful variants; fold full-page context
into a <details> block.>

## Issue link
<One trailer per issue this PR resolves, and it MUST use a closing verb —
"Fixes #<n>", "Closes #<n>", or "Resolves #<n>". A bare "#<n>" or "Related: #<n>"
links the issue and closes NOTHING, so the work merges and the issue stays open
forever. Confirm the host actually resolved it:
  gh pr view <n> --json closingIssuesReferences
If this PR deliberately resolves no tracked issue, delete the trailer below and
declare that instead, as its own line starting at column 0, in the exact form
item 7 of the PR description contract specifies. Replace this whole instruction
block either way — leaving it in place is not a declaration, and the readiness
gate does not accept it as one.>

Fixes #<n>
