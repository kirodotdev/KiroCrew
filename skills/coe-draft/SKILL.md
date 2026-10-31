---
name: coe-draft
description: Turn raw incident notes into a structured Correction-of-Error / postmortem draft that follows a proven blameless template — customer-perspective title, standalone summary, quantified impact, trigger-anchored timeline, Five Whys covering cause AND detection AND resolution, and owned action items — so the author starts from a rigorous skeleton, not a blank page.
triggers: coe, postmortem, post-mortem, correction of error, root cause, rca, incident writeup, incident review
---

# Correction-of-Error / Postmortem Drafting

You reshape messy incident material into a blameless postmortem draft that
follows the section order below. You never invent facts — missing detail
becomes an explicit `[TODO: ...]` placeholder, never a guess. This is a draft
for a human to verify and finish; say so if the inputs are thin.

## Global rules (apply to every section)

- **Blameless.** Describe systems and actions, never individuals by name. Use
  roles ("the on-call", "the deploy") if an actor must be referenced.
- **Be specific; no mental math.** "46 minutes, ~3,120 requests failed," not
  "a significant number for a while." Ban the words *significant, some, a few,
  a percentage* where a real figure belongs.
- **Spell out every acronym on first use.**
- **Never fabricate** timestamps, metrics, or causes. Missing = `[TODO]`.

## Sections, in order

### 1. Title
State the problem from the **affected party's perspective, not the cause** —
"Users unable to submit orders," not "Cache misconfig caused outage." Most
incidents have several causes; none of them belongs in the title, and leading
with a cause pre-judges the analysis.

### 2. Summary
Must stand alone — write it as if it will be forwarded to a senior exec with no
other context (it often is). Who was impacted, when, where, how; how long
discovery and resolution took; how it was mitigated; how recurrence will be
prevented. Basic facts only — details live below.

### 3. Metrics / Graphs
Reference the metrics that show the impact. For each: what it depicts, the
axes and units, the relevant threshold/baseline, and the sampling interval.
Mark the impact start/end. `[TODO: attach graph of <metric> over <window>]`
where an image belongs. A few clear graphs beat many confusing ones.

### 4. Impact
Quantify precisely: duration (start → detection → mitigation → resolution) and
scope (what functionality, how many users/requests, any data effect). Describe
*how* the affected parties experienced it. Do not name specific customers; do
not speculate about their business impact — only the effect your own systems
had.

### 5. Timeline
Chronological table: `HH:MM TZ` | event | how it was known. **Start at the first
trigger** (e.g. the bad deploy), not when someone got paged. Use one consistent
timezone — prefer UTC, or drop the ambiguous middle letter ("PT"). Bold the
detection and mitigation milestones. Any gap over ~10–15 min must be explained
in the Five Whys.

### 6. Incident response analysis
Deep-dive the window between trigger and diagnosis: how the event was detected,
how long to engage the right people, how long to identify root cause. Every gap
here should produce a corrective action item in section 8.

### 7. Five Whys
Ask "why" until you reach **systemic** causes — process, design, tooling — not
a person and not "we should be more careful" (a wish, not a cause). It may take
more or fewer than five; branch when there are multiple causes. This section
must answer three questions, not one:
- **Root cause:** why did the issue occur?
- **Detection:** why did it take so long to discover?
- **Resolution:** why did it take so long to resolve?
Do not stop at "human error" (keep going until a system change would make the
error impossible) or at "a procedure was missing/weak" (document how to create
or automate it).

### 8. Lessons learned
What went well (detection speed, tooling, blast-radius limits) and what gaps
the analysis exposed. Each gap should trace to an action item.

### 9. Action items
Table: action | type (prevent / detect / mitigate) | owner `[TODO]` | due
`[TODO]` | tracking ref `[TODO]`. Prefer actions that make the failure
impossible or auto-detected over "be more careful." Root-cause actions get top
priority. Every action must trace to a root cause or a "went wrong" finding.

### 10. Related items
Links to the originating ticket(s) and any related reviews `[TODO]`.

## If inputs are thin
Ask for the three highest-value pieces first: the timeline (from the trigger),
the quantified impact, and how it was detected.
