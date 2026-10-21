---
title: Crewmate page ambient motion -- a crewmate's own Dashboard page may run subtle, constant ambient motion by default, held still only for a reader who set reduced motion
status: accepted
author: rayrayxu, with kirocrew-worker
created: 2026-10-21
last-audited: 2026-10-21
audited-at: 4533b1eb5b
doc-pr: null
implementation-prs: [22533]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Crewmate page ambient motion

- Status: accepted. This is the product owner's decision, given 2026-10-21 by
  rayrayxu. Nothing of it is on main: the text that carries it is in
  [#22533](https://github.com/kirodotdev/KiroCrew/pull/22533), open. This
  document lands on its own first, because it changes a rule of an implemented
  document, and the First Principles lane reads that decision off the base.
- Amends, in this document so that implemented document is not edited:
  [rfc-living-ui.md](rfc-living-ui.md) §5.1 rule L, the bullet "On a dashboard,
  motion happens **on change only**", and §10's matching risk row, for a
  crewmate's own Dashboard page only. Everything else in rule L stands,
  including the reduced-motion bullet, which this document keeps and states
  more exactly.
- Claims about today's code were checked at `4533b1eb5b` (main), and about
  #22533 at its head `07b256f8a2`.

## 1. Problem

`rfc-living-ui.md` treats every dashboard as a page watched all day: motion only
on change, then still. That suits the built-in pages, which are dense status
views. A crewmate's own Dashboard page is different. It is the crewmate's face to
its person, written around one goal, and pod rounds showed that a page held to
"still between changes" reads like a form: correct, and nobody comes back to it.
Subtle motion that runs all the time the page is in view (a slow drift in a
background, a breathing accent on what is running), not only when a value
changes, is what makes it feel alive.

The opposite mistake is also live. A crewmate told nothing about reduced motion
either ignores the setting or, told to be careful, drops motion for everyone.
The rule has to say both halves: motion by default, and still for exactly the
readers who asked for still.

## 2. Goals

1. A crewmate's own Dashboard page may run subtle ambient motion and animation
   constantly while it is in view, by default, not only on a change, beside the
   data-driven motion `rfc-living-ui.md` already allows.
2. A reader whose system sets `prefers-reduced-motion: reduce` gets a still page:
   every value jumps to its target, ambient layers hold still, no entrance plays,
   and every envelope is 0. The page still answers input.
3. No other reader loses motion because some readers set reduced motion.

## 3. Non-goals

- The built-in pages, widgets and explainers. Rule L stands for them unchanged.
- An idle tour on a Dashboard page. It stays an explainer's only.
- Picking a style. `rfc-living-ui.md` stays style-neutral, and so does this.

## 4. Design

### 4.1 What a crewmate is told

The rule lives in the texts a crewmate reads before it writes its page:
`dashboard_templates.page_look.PAGE_LOOK`, which the first-build line
(`goal_reminder.DASHBOARD_BUILD_LINE`) and the `dashboard_save` view description
carry verbatim, and the dashboard skill's "Make it feel alive" and "Reduced motion
is for the reader who set it" paragraphs.

| Reader | What the page does |
|---|---|
| No reduced-motion setting | subtle ambient motion running constantly while in view; data-driven motion on top of it on a change |
| `prefers-reduced-motion: reduce` | values jump to their targets; ambient layers still; no entrance; envelopes 0 |

### 4.2 Cost bounds that stand

Ambient motion is light on resources and never repeats layout work. It pauses
while the page is hidden and while the figure is offscreen, as rule L's loop
already does (`IntersectionObserver`, `rootMargin: "120px 0px"`). These bounds
are what keeps an all-day page cheap, and this document does not relax them.

### 4.3 What it amends

Rule L's bullet "On a dashboard, motion happens on change only ... then the page
is still" reads, for a crewmate's own Dashboard page: "subtle ambient motion runs
constantly while the page is in view, and a change adds data-driven motion on
top; both pause while hidden or offscreen". §10's row "dashboards get motion on change only" is amended the same
way, for that page only.

## 5. Migration plan

| Phase | PR | Exit criteria |
|---|---|---|
| 1. The texts | #22533 | `PAGE_LOOK`, the build line, the `dashboard_save` view text and the skill invite subtle ambient motion that runs constantly while in view, not only on a change, and state the reduced-motion rule for the reader who set it; none says to drop motion for everyone (`test_dashboard_page_texts.py`). |

## 6. Backward compatibility

Additive for pages: a page written to rule L as it stands still meets this one.
Nothing is removed from the toolkit.

## 7. Security considerations

None. Motion is page markup and script inside the same frame and the same save
checks as today.

## 8. Alternatives considered

- **Keep "on change only" for crewmate pages.** Rejected: pod rounds showed still
  pages read like forms, which is the problem `rfc-living-ui.md` itself set out
  to fix.
- **No reduced-motion wording at all.** Rejected: a crewmate then ignores the
  setting, and the readers who need still lose it.
- **Still for everyone by default, motion on request.** Rejected: it takes motion
  from every reader to serve the ones the setting already serves.

## 9. Open questions

None.
