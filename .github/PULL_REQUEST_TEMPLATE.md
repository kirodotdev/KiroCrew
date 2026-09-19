<!-- Fill in each section below. Omit a section only when it is genuinely not
     applicable, and say so (e.g. "N/A — ..."). Keep the diff and this
     description in sync: every claim here must be supported by the diff. -->

## Problem / Motivation

<!-- Bug fix: the concrete symptom — what is broken or missing, ideally what the
     user observes.
     New feature / enhancement: the gap, use case, or opportunity this addresses
     — what a user cannot do (or does awkwardly) today. -->

## Why it matters

<!-- Impact if this is left undone: for a fix, who is hit by the bug and how
     badly; for a feature, the user/business value it unlocks. -->

## What changed (motivation → approach → change)

<!-- A short chain of thought so the reader sees *why this is the right change*,
     not just what changed:
     - Bug fix: observed symptom → underlying root cause → the specific change
       that addresses that cause.
     - New feature / enhancement: goal → the approach/design you chose (and why,
       over the alternatives you considered) → what you actually built.
     - Product-shape change (a changed default, what a loop/monitor/agent/command
       does by default, a removed or replaced user-facing capability): link the
       RFC under docs/request-for-change/ that records the decision. It must
       already be on main with a non-draft status; an RFC shipped in this PR, or
       whose status this PR flips, does not count. Without one the First
       Principles lane BLOCKs until a maintainer records the decision with
       `/ai-review override first-principles <head-sha>: <reason>` (same-repo
       PRs only -- on a fork PR the override is not consumed: merge the RFC
       first, or ask a maintainer to push the branch to this repository). -->

## Backwards compatibility

<!-- Does this change REJECT, RENAME or REMOVE anything main currently accepts
     (new required field, stricter validator, narrowed type, removed kind,
     renamed key)? One of:
       Compatible: <one line on why nothing that works today stops working>
     or:
       Breaking: <what stops working>. Writer sweep on origin/main @ <sha>:
       <every writer/caller found and how each is handled>.
     A Breaking change tightens one contract; the writers it breaks may live in
     other open PRs, so re-run the sweep on fresh main before the last push. -->

## Tests

<!-- Automated tests added/updated and the behavior each one locks in. -->

## Manual verification

<!-- Manual steps performed or still required where unit tests fall short
     (integration paths, UI, external services). State "N/A — unit coverage
     sufficient" only when genuinely true, with a one-line why. -->

## Screenshots / video

<!-- MANDATORY for any user-visible UI change (new/changed panels, components,
     layouts, themes); delete this section otherwise.

     - Show each affected surface in its meaningful variants (e.g. desktop vs
       browser, empty vs populated, light vs dark).
     - Prefer a short video/GIF when the change involves motion or a multi-step
       flow (animations, transitions, interactions) — a still image cannot
       prove those.
     - Upload media as GitHub attachments; do not commit it to the repository.
       Write ordinary local paths in this section and pass the same files to
       `gh pr create|edit --attach <path>` (gh >= 2.99): each path is rewritten
       in place to a permanent https://github.com/user-attachments/assets/...
       URL that survives force-pushes, branch deletion and merge. Dragging the
       file into this box in the web UI produces the same URL. Attach before
       your last push: editing this description later starts no review (re-run
       the UX Review workflow, or push again, and it reads the current text).
         ![alt](./evidence/after.png)
         ![](./evidence/demo.mp4)   <- alone in its paragraph renders as a player
       Limits: 10 MB per image/GIF, 100 MB per video.
     - Non-media evidence is neither attached with --attach nor committed.
       Text (a provenance JSON, a perf baseline, an assertion dump) goes in
       a fenced code block in a PR comment (65,536 characters max). A
       document (PDF, docx, zip) is dragged into a PR comment in the web
       UI (25 MB max; it becomes a permanent user-attachments/files URL).
       Link that comment's permalink from any spec that cites it.
     - Put the two or three most telling shots inline; fold full-page context
       into a <details> block. -->

## Related Issues

<!-- Link to relevant issues, e.g. Fixes #123 -->

## Pattern harvest

<!-- REQUIRED for fix/revert PRs (PR Hygiene checks that this section is
     present); delete this section otherwise. Every fixed defect is a chance
     to stop its whole class: say whether this one generalizes.

     One of:
       Rule candidate: <semgrep | lint | review-prompt | agents-md>
       Pattern: <one line, e.g. "sentinel return value used in boolean context">
     or:
       Not generalizable: <one line on why this defect is a one-off> -->

## Checklist

- [ ] At most two commits (one is the norm), with a Conventional Commits title (`feat|fix|docs|refactor|perf|test|chore|ci|build|revert: ...`)
<!-- If your branch goes stale, REBASE it. Plain-clicking the "Update branch"
     button on the PR page, or merging the base branch in, adds a merge commit,
     which counts toward the limit above and will fail PR Hygiene on a PR that
     was otherwise green. That button's dropdown carries an "Update with rebase"
     option, which is safe. Pick that, or run:

       git fetch origin && git rebase origin/<base branch>
       git push --force-with-lease -->
- [ ] Existing tests pass and new tests added for new functionality
- [ ] Self-review completed; code follows project style guidelines
- [ ] Documentation updated (if applicable)
- [ ] No secrets, credentials, or internal references in the diff

## Contribution License Agreement

<!-- PLACEHOLDER: The exact CLA wording will be supplied by OSPO before the first public PR.
     Do not invent CLA text — it will be added here once Legal provides it. -->
