---
title: Crewmate page on a goal wake -- the goal-loop wakes build a crewmate's first Dashboard page, and offer once on a card before replacing a template
status: accepted
author: rayrayxu, with kirocrew-worker
created: 2026-10-21
last-audited: 2026-10-21
audited-at: c428ea050d
doc-pr: null
implementation-prs: [22533, 22560]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Crewmate page on a goal wake

- Status: accepted. This is the product owner's decision, given 2026-10-21 by
  rayrayxu. Nothing of it is on main: the first build is in
  [#22533](https://github.com/kirodotdev/KiroCrew/pull/22533) and the
  ask-once offer in [#22560](https://github.com/kirodotdev/KiroCrew/pull/22560),
  both open. This document lands on its own first, because both add a job to
  goal-loop turns, and the First Principles lane reads that decision off the base.
- Amends, in this document so the accepted and partial documents are not edited:
  [rfc-crewmate-goals.md](rfc-crewmate-goals.md) §3.5 (each cadence has one job)
  and the goal-setup turns it describes;
  [rfc-crewmate-dynamic-dashboard.md](rfc-crewmate-dynamic-dashboard.md), which
  owns the page, its apply and its lock, and never says who starts a crewmate's
  first page.
- Claims about today's code were checked at `c428ea050d` (main). Claims about the
  two PRs were checked at their heads, `88c0d966ab` (#22533) and `6634af4ea2`
  (#22560).

## 1. Problem

A crewmate with a goal and confirmed milestones has a Dashboard tab, and on main
that tab shows the suggestions page until someone puts a page there. Nothing tells
the crewmate it can. The person has to know to ask, in chat, for a page, and most
do not. The crewmate already knows what its page should be about: its goal and its
milestones. It is never woken to use that.

A crewmate whose tab shows a template has the opposite problem. The template is the
person's choice, so a wake must never replace it. But it could still be a better
fit to have a page built around the crewmate's own goal, and the person is never
asked.

## 2. Goals

1. A crewmate whose Dashboard tab shows nothing of its own builds and applies its
   first page on its own, once it knows what the page is about: a goal and
   confirmed milestones.
2. A crewmate whose tab shows a template never replaces it unasked. It asks once,
   on a card. Yes builds the page; no or expiry changes nothing, and it is not
   asked again.
3. Neither happens under the person's **Lock dashboard layout** switch.
4. Both ride a wake the goal loop already runs. Neither adds a turn or a schedule.

## 3. Non-goals

- Changing what a page may contain, how it is checked, or how apply and rollback
  work. That is `rfc-crewmate-dynamic-dashboard.md`.
- Motion, focus and reduced-motion rules for a page. That is `rfc-living-ui.md`,
  which this document does not change.
- Rebuilding a page on a later wake. After the first page, the crewmate changes it
  only when the person asks in chat.
- Removing or migrating the template catalog.

## 4. Design

### 4.1 Which wakes, and when

Three goal-loop turns can carry one extra line, `goal_reminder.dashboard_line`:

| Wake | Built by |
|---|---|
| Daily Goal Reminder | `goal_reminder.reminder_prompt(..., dashboard=)`, daily cadence only |
| Goal setup (kickoff) | `goal_setup.kickoff_prompt(..., dashboard_line=)` |
| `[Milestones confirmed]` | `goal_setup.milestones_prompt(..., dashboard_line=)` |

The Weekly report and Monthly review never carry it.

`dashboard_line(slug, rec)` returns one of three things, checked in this order:

| Case | Line |
|---|---|
| No goal, or no confirmed milestones (`knows_its_page`) | none |
| Layout locked (`template_instance.is_locked`) | none |
| A package of the crewmate's own is bound (`dashboard_agentic.bound_package`) | none |
| Tab shows a template page (not `tab_shows_suggestions`) | the offer line (§4.3), once |
| Tab shows the suggestions page | the build line (§4.2) |

A lookup that fails gives no line. A milestone is stored only once the person
confirms it, so a stored one is a confirmed one.

### 4.2 Nothing on the tab: build it

The build line is `goal_reminder.DASHBOARD_BUILD_LINE`. It tells the crewmate to
build its page with the `dashboard` skill, `dashboard_save` it, `dashboard_apply`
it, and then post one short message that the page is on its Dashboard tab. No
preview and no card: nothing the person chose is replaced, and the page goes live
on apply as `rfc-crewmate-dynamic-dashboard.md`'s amendment of 2026-10-15 already
records for the owner's own package.

It happens once. After the apply a package is bound, so the next wake gets no line.
The person changes the page by asking the crewmate in chat, and stops all of it
with the lock switch.

**This amends `rfc-crewmate-goals.md` §3.5.** The Daily Goal Reminder's job stays
"take a fresh reading of each key result, then continue with the next task". While
the line is present, and only then, the next task is building the page. Kickoff and
`[Milestones confirmed]` get the same line on the same terms.

### 4.3 A template on the tab: offer once

The offer line is `page_offer.OFFER_LINE`, naming the template. It is given only to
a crewmate with the `kirocrew-mate` tools, because it asks the crewmate to call
`offer_dashboard_page` once. That tool raises one `crewmate.dashboard_page` change
card ("Want me to build a Dashboard page around your goal?"), with one row naming
the template it would replace. Raising it writes nothing to the page.

- **Self-only.** The kind is in `SELF_ONLY_KINDS`: `list_change_kinds` leaves it
  out and `propose_change` refuses it.
- **Asked once.** `crew-panels/<slug>.page-offer.json`, outside the crewmate's own
  shell, is written after the card is built and before it is shown. A yes, a no and
  an expiry all count. A record that cannot be written refuses the card; an
  unreadable record counts as asked. The record holds only `replaces`.
- **Yes.** Its one apply step, `POST /api/members/{slug}/dashboard/page-offer`,
  owner-only and refused unless the card set it, writes nothing and wakes the
  crewmate once to write, save and apply its page, then post the same one message.
  A locked layout refuses the apply. One `dashboard_rollback` brings the template
  back.
- **No, or expiry.** Nothing changes, and no later wake offers again.

### 4.4 The lock

`Lock dashboard layout` on Profile already makes apply and rollback answer
`dashboard_locked`. Under it no wake carries either line (§4.1), so a crewmate is
never told to do something the lock refuses.

## 5. Migration plan

| Phase | PR | Exit criteria |
|---|---|---|
| 1. First build | #22533 | A crewmate with a goal, confirmed milestones and the suggestions page gets `DASHBOARD_BUILD_LINE` on the three wakes; with no milestones, a lock, a bound package or a template it gets none (`test_dashboard_default_by_slug.py`). |
| 2. Ask once | #22560, after #22533 | A template tab gets the offer line, never the build line; one card per crewmate; yes wakes once and writes nothing itself; no and expiry write nothing; no line after asking (`test_dashboard_page_offer.py`). |

Each phase can be dropped alone: without phase 2, a template tab simply gets no line.

## 6. Backward compatibility

Additive. No route, key, kind or string is removed. A crewmate with no goal, no
milestones, a lock or a page of its own sees no change. A template page keeps
rendering.

## 7. Security considerations

The page a crewmate writes passes the same save checks and renders in the same
frame as any page today; this document adds no new reach. The offer's apply route
is owner-only and refused unless the card's own hook set it, so the crewmate cannot
start the yes wake itself. The asked-once record sits outside the crewmate's shell,
so the crewmate cannot reset it.

## 8. Alternatives considered

- **A card for the first build too.** Rejected: an empty tab replaces nothing the
  person chose, and the card would be one more question with only one sensible
  answer.
- **Replace a template on a wake.** Rejected: it undoes the person's choice.
- **A new schedule for page building.** Rejected: the goal loop's wakes already run
  at the right moments, and a new schedule is a new thing to turn off.
- **Rebuild the page on every wake.** Rejected: the person would see their page
  change under them.

## 9. Open questions

None.
