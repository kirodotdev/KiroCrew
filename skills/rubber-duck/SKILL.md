---
name: rubber-duck
description: Adversarial "rubber duck" review that turns explaining-out-loud into a hallucination check. The main session is the PRESENTER (it did the work — a design doc, investigation, or analysis — and holds the real reasoning) and reconstructs the topic to a LISTENER — a spawned subagent pinned to a DIFFERENT-vendor model that has NOT seen the reasoning and interrogates it adversarially, hunting inconsistencies, gaps, unsupported magnitudes, and claims stated as fact but actually inferred. Goal — artifacts that are complete, gaps that are honest, findings that are truthful. Triggers include "rubber duck this", "rubber duck review", "explain this to a skeptic", "reconcile my findings", "is my doc honest".
version: 0.1.0
tags: [skill, rubber-duck, review, cross-vendor, spawn_run, subagents, reconciliation, anti-hallucination]
---

# Rubber Duck Review

## Overview

The classic debugging trope, aimed at hallucination. The **main session is the
Presenter** — it did the work and holds the real reasoning (including the hedges it
never wrote down). It reconstructs the topic *out loud, in its own words*, to a
**Listener**: a spawned subagent pinned to a **different-vendor** model that has NOT
seen the reasoning and interrogates it adversarially. Two forces do the work:

1. **The Feynman effect** — reconstructing the argument from scratch surfaces the
   Presenter's *own* hand-waving before the Listener even replies.
2. **An unstaked skeptic** — the Listener has no investment in the conclusion, so it
   catches what the artifact's own self-review rationalized past.

This is a **dialogue**, not a static-artifact review: the Presenter reconstructs the
argument live, and the divergence between the reconstruction and the written artifact
is itself a finding.

## When to use / when NOT

**Use** before shipping a findings doc, design, RCA, or investigation you want to be
honest — anything where a "confirmed" might be hiding an unproven leap, or a magnitude
might be hand-waved. **Do NOT use** for simple lookups or routine turns: it costs one+
extra model run per round *and* real Presenter effort. A deliberate, occasional move.

## Roles

- **Presenter — you, the main session.** Reconstructs the topic from understanding
  (NOT copy-paste), answers the Listener's probes honestly, builds the reconciliation
  ledger. *Ideal: reconstruct before re-reading the artifact, so the divergence
  between what you believe and what you wrote is genuine; at minimum, articulate in
  your own words rather than pasting the doc.*
- **Listener — a spawned subagent.** Cross-vendor, large-window, lean. Sees the
  Presenter's explanation plus the original user request (the *ask*, for grounding —
  not the artifact or the reasoning), so it attacks the claims *as articulated*, which
  is what exposes explanation-vs-artifact drift. Residual gap: a self-consistent
  fabrication the Presenter also grounds falsely can survive — the blind Listener bounds
  overclaim and inconsistency, not a mutually-consistent hallucination.

## Procedure (you are the Presenter)

1. **Reconstruct** — explain the topic in your own words: objective, key observations,
   the mechanism/claim, the eliminations you ran, and the honest status. Compress to
   the load-bearing claims (see constraints).
2. **Spawn the Listener** — ONE `spawn_run`, `model=` a large-window model from a
   **different vendor** than your own family. `task` = the Listener charter (below)
   with your explanation *and the original user request* (the *ask*, for grounding)
   filled in. State the topic **neutrally** — do NOT signal which claims you think are
   weak or what you expect it to find; a led listener just mirrors you.
3. **Wait** for the `[Subagent completion event]`. Do not answer for it.
4. **Answer with evidence, not prose** — for each probe, answer by CHECKING a real
   signal (data, code, source) rather than re-justifying the original wording
   (re-justifying re-anchors you to the error — CoVe). Concede ONLY when the probe shows
   a concrete gap or you can't ground the claim; do NOT cave to pushback alone (models
   frequently reverse a correct answer under mere challenge, absent new evidence). Mark
   each **HELD** (survived *and* grounded) or **CONCEDE**; a probe you can't ground is
   itself a finding. In the reconciliation ledger a CONCEDE maps to **DOWNGRADED** (a
   weaker-but-true wording exists) or **GAP-FLAGGED** (the hole needs new evidence).
5. **Iterate to convergence** — keep running rounds while each surfaces a NEW ungrounded
   claim, gap, or contradiction. Each round re-spawns the same-model Listener with a
   **distilled** transcript (open findings + live probes, not the verbatim dialogue — a
   raw transcript blows the 5000-char `task` cap by round 2-3). STOP when a full round
   yields nothing new *and* every prior finding is resolved (HELD-grounded, DOWNGRADED,
   fixed, or logged as an honest OPEN gap). Convergence is the **Listener running dry —
   not you declaring "done"** (a Presenter's self-satisfaction is the very blind spot
   this skill defeats). Safety cap 8 rounds; hitting it unconverged IS a finding — report
   the unresolved OPEN items. Agreement is never a stop signal; only *no new grounded
   objection* is.
6. **Reconcile** — build the ledger (below) against the ACTUAL artifact and propose
   edits.

## Constraints the live prototype exposed — READ THESE

- **Listener must be LEAN + LARGE-WINDOW.** Subagents inherit the full injected KiroCrew
  context (skill *descriptions*, memory, lessons — not full skill bodies, but still
  large). A small-window model **overflows its context window before it can read your
  task** (a real prototype failure), so pin the Listener to a large-window model from a
  different vendor than your Presenter's family. `spawn_run` has no `minimal_context`
  flag today.
- **`spawn_run` task cap = 5000 chars.** Charter + explanation must fit. If the topic is
  large, **compress** the explanation to its essential claims — do NOT paste the whole
  artifact. Compression is a feature: stating the argument in a few hundred words *is*
  the rubber-duck effect.
- **Cross-vendor is the point.** A same-family Listener *tends to* rationalize the way
  the Presenter does (same-family models often diverge too, but cross-vendor maximizes
  failure-mode diversity). Discover the live menu with `kiro-cli chat --list-models --format
  json`; prefer large `context_window_tokens`, pick a different vendor than yours.

## Listener charter (the `task`, `{TOPIC_EXPLANATION}` filled in)

```
You are the LISTENER in a "rubber duck review" — an adversarial skeptic. A presenter
explained a topic to you. You have NOT seen their doc or data — only the explanation
and the original ask below. Force honesty by LOCATING defects — name the specific claim, number, or step
that is suspect and say why; do NOT fix or rewrite anything (error-finding, not fixing,
is your job). Judge only factual support and logical validity — ignore length,
formatting, fluency, and confident tone. Separate OBSERVED from INFERRED and challenge
every inference; demand the counterfactual for any causal claim; flag n=1 / single-
sample bases; refuse "confirmed" for anything whose stated objective has not actually
moved in an experiment; attack estimated or hand-waved magnitudes; hunt internal
contradictions (a number in one place that undercuts another). Don't be agreeable. If
something is genuinely solid, say so in one line and move on — spend your effort on the
soft joints.

TOPIC EXPLANATION:
"""
{TOPIC_EXPLANATION}
"""

ORIGINAL USER REQUEST (the ask — for grounding only; NOT the artifact or reasoning):
"""
{ORIGINAL_ASK}
"""

Produce your interrogation, structured EXACTLY as:
1. HARDEST QUESTIONS — ranked numbered list. Each targets a specific claim and states
   what a satisfying answer must contain.
2. SUSPECTED WEAKNESSES — bullets, each tagged with ONE of: [OVERCLAIM] / [GAP] /
   [INCONSISTENCY] / [UNSUPPORTED-MAGNITUDE] / [MISSING-COUNTERFACTUAL] / [SAMPLE-SIZE].
   State the claim and why it's soft.
3. VERDICT — one paragraph: is the presenter's honesty label accurate, or does it still
   overclaim somewhere? Name the single weakest joint that, if it broke, would collapse
   the most of the thesis.
Be concise and surgical. No preamble.
```

## Reconciliation ledger — the Presenter's deliverable

Compare the Listener's findings against the ACTUAL artifact and classify every
challenged claim, one row each, with a proposed edit:

- **HELD** — survives the probe *and* is grounded; note why the objection fails. A
  coherent-but-ungrounded claim is NOT held — downgrade or gap-flag it (a fluent
  explanation must not launder an unsupported claim into HELD).
- **DOWNGRADED** — overstated; give the corrected, weaker-but-true wording
  (e.g. "confirmed" → "leading hypothesis"; a p99.9-vs-p99.9 ratio → a paired ratio).
- **GAP-FLAGGED** — a real hole; say what's missing and whether it's answerable from
  data on hand vs needs a new experiment.
- **CONTRADICTED** — internally inconsistent; name both sides.

## Edit mode

**Propose-only by default.** Present the ledger + proposed edits; apply to the artifact
only on explicit user confirmation — findings docs are high-stakes, keep the human in
the loop. "Apply" mode edits via `artifact_update` / file write *after* sign-off.
