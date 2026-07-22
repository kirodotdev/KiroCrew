---
name: learn-from-sage
description: Detection-gap (miss) analysis for Code Review Sage. Learn from shipped fixes, acted-on human comments, and design outcomes to close reviewer blind spots. Inline during review stages a candidate; a human triggers a one-shot AI consolidation into the live ruleset.
always: false
---

# Learn from Sage — detection-gap (miss) analysis (V2, file-centric)

Learning is **not** summarizing what Sage found. It is finding issues that
**leaked past review and shipped**, then working backwards to close the gap so
the reviewer catches the next one.

Two files, one rule each:
- **`learned-patterns.md`** — the canonical, consolidated ruleset. The **only**
  file a review loads as heuristics. Human-editable.
- **`learned-patterns.candidate.md`** — append-only **staging** for new learnings.
  Reviews do NOT read it. It is merged into `learned-patterns.md` only when a
  human triggers **consolidation** (a one-shot AI merge), after which it's cleared.

## Self-heal (first)

```bash
python3 ~/.kirocrew/apps/code-review-sage/sage_lib/store.py --ensure
python3 ~/.kirocrew/apps/code-review-sage/sage_lib/learning.py seed   # no-op if already seeded
```

## Admissible sources only (no self-poisoning)

Learn **only** from human-validated, ground-truth signals:
- `fix_introduce` — a real bug shipped and was fixed (fix → introducing change).
- `human_comment` — a reviewer comment that was acted on or recurred.
- `design_outcome` — a recorded design-discussion outcome (e.g. a feature reverted).
- `import` — a pattern imported from another user.

Sage's **own draft findings are NOT a source** unless a human published/accepted
them (then they become a `human_comment`). The reviewer learns from what reality
proved it missed, never from its own opinions. `sage_lib/learning.py stage` enforces
this — an inadmissible source raises.

## Inline miss-analysis → STAGE (during review, when the change is a fix)

When a change being reviewed has `is_fix == true`, run this **as part of that
review** (not a separate pass):

1. **Trace to the introducing change** — `git log --follow` / blame the fixed
   lines back to the commit (and the change) that introduced the defect.
2. **Miss analysis — why wasn't it caught?** Would any of the 9 dimensions have
   flagged the *introducing* change? If not, **which dimension was blind, and
   what specific check would have caught this class of defect?**
3. **Quality gate — keep it high-level and reusable (generalize, don't memorize).**
   Admit a lesson only if it is general (not a one-off), non-trivial (not a
   lint nit), and fits a dimension. Then write it as **high-level guidance a
   reviewer can apply to future, unrelated changes**, under these hard limits:
   - **`guidance`: 1–2 short sentences naming the defect *class* and the check.**
     No code snippets, no function/variable/file names, no CR numbers. It must read
     as a durable review heuristic, not a description of this one bug. This is the
     **whole rule** — there is no symptom line and no example. If a rule would only
     be understandable with an anecdote or a concrete example, the guidance is too
     vague: sharpen it so it stands on its own, or drop it.
   If the guidance only restates this bug ("CR-123 forgot to reset flag X"), it is
   too specific: lift it to the class ("an early-return path that skips a guard
   reset leaves a stale invariant — check every early return restores invariants")
   or drop it. When in doubt, prefer fewer, broader rules over many narrow ones.
4. **Stage** it (cheap, no model merge yet):
   ```bash
   python3 ~/.kirocrew/apps/code-review-sage/sage_lib/learning.py stage \
       --file /tmp/pattern.json --source fix_introduce [--namespace <name>]
   ```
   The pattern JSON carries: `title, scope (common), dimension, impact, guidance`.
   The `guidance` is the entire durable heuristic — code-agnostic and self-contained.
   This **appends to the candidate file** — it does not touch the live ruleset.
   Omit `--namespace` to stage into the default namespace.

## Consolidate (human-triggered — the one-shot AI merge)

When the human asks to consolidate (or the app's "Consolidate" button routes here):

1. Read both files:
   ```bash
   cat ~/.kirocrew/apps/code-review-sage/data/learnings/common/learned-patterns.md
   cat ~/.kirocrew/apps/code-review-sage/data/learnings/common/learned-patterns.candidate.md
   ```
2. In **one pass**, produce the merged ruleset. The goal is a **lean, high-level,
   code-agnostic** rulebook a reviewer can skim in seconds — not an exhaustive log:
   - **Merge near-duplicates** aggressively into one sharpened rule. **Compress**:
     rewrite verbose guidance down to 1–2 high-level sentences and strip any code
     snippets, identifiers, file/function names, or CR numbers — a pattern is
     guidance-only, so that detail is simply dropped, not relocated.
   - **Drop low-value or stale one-offs.** A rule that only ever applied to a single
     bug and names no reusable defect *class* does not earn a slot.
   - **Resolve conflicts** so safety-/posture-preserving guidance wins; never
     silently drop a distinct safety/correctness guard.
   - Keep the on-disk pattern format: `### title <!-- scope: --> <!-- impact: -->`
     followed by the guidance line only (no Symptom, no Example). Favor a tight
     ruleset that stays roughly stable in size across consolidations over one that
     grows every time.
3. Write the merged markdown to a temp file and apply it atomically — this
   replaces `learned-patterns.md` and clears the candidate:
   ```bash
   python3 ~/.kirocrew/apps/code-review-sage/sage_lib/learning.py consolidate \
       --merged-file /tmp/merged-learned-patterns.md
   ```
   `consolidate` refuses empty content (never wipes the ruleset) and records a
   `consolidations.jsonl` audit entry.
4. **Human gate via the file viewer.** Before consolidating, the human may open
   and edit the candidate directly:
   `~/.kirocrew/apps/code-review-sage/data/learnings/common/learned-patterns.candidate.md`
   After consolidating, show the updated
   `~/.kirocrew/apps/code-review-sage/data/learnings/common/learned-patterns.md`
   so they can review/edit the result (the dashboard opens these paths in the
   file viewer).

Inspect staging anytime:
```bash
python3 ~/.kirocrew/apps/code-review-sage/sage_lib/learning.py list-candidate [--namespace <name>]
python3 ~/.kirocrew/apps/code-review-sage/sage_lib/learning.py list-patterns [--namespace <name>]
python3 ~/.kirocrew/apps/code-review-sage/sage_lib/learning.py clear-candidate [--namespace <name>]
python3 ~/.kirocrew/apps/code-review-sage/sage_lib/learning.py list-namespaces
python3 ~/.kirocrew/apps/code-review-sage/sage_lib/learning.py list-for-review  # union of active namespaces
```

> Namespaces are supported: learnings are grouped by namespace. The `default`
> namespace maps to `common/`; user namespaces live under `namespaces/<name>/`.
> Pass `--namespace <name>` to target a specific ruleset when staging or
> consolidating, and set `review.active_namespaces` in config.json to control
> which namespaces a review loads (their patterns are unioned via `list-for-review`).
