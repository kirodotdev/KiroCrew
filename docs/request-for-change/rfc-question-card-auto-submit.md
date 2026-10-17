---
title: Question card submits on the pick that finishes it
status: draft
author: Pearcekieser
created: 2026-10-07
last-audited: 2026-10-07
audited-at: 781294256f
doc-pr: 17584
implementation-prs: [17542]
tracking-issues: [17522]
supersedes: []
superseded-by: []
---

# RFC: Question Card Submits on the Pick That Finishes It

> **Status:** `draft`. Acceptance is requested from a maintainer; the status
> flips to `accepted` when one records it in §6 with the date. Nothing is on
> main. Verified at `781294256f`: in `website/src/components/QuestionCard.tsx`
> the card's answers leave only through `handleSubmit`, which the Submit button
> and `Enter` on the last page call, so a single-select pick that answers the
> last open question lights up and waits for Submit. The implementation is
> [#17542](https://github.com/kirodotdev/KiroCrew/pull/17542). This document
> lands first because it changes how every single-select `ask_question` card
> ends, and the First Principles lane reads that decision from the base branch.

## 1. Summary

When a single-select click on the last page answers the last open question, the
card submits 200 ms later with no Submit click. A one-question single-select
card, the common case, then takes one click instead of two. Every other way of
finishing a card keeps the Submit step.

This amends [rfc-question-card-pager.md](rfc-question-card-pager.md) §3.2, which
puts Submit at the end of the walk. That document is accepted and is not edited
here; this one records the amendment and its own acceptance separately.

## 2. Background

The pager already moves to the next unanswered question after a single-select
pick (pager §3.2 and §3.5). On the last question the pick only marks the option,
and the user then has to find and click Submit. For a card that asks one
either-or question, the second click carries no information: the answer is
already chosen and nothing else is open. Issue
[#17522](https://github.com/kirodotdev/KiroCrew/issues/17522) asked for the pick
itself to send the answer.

## 3. Design

The completing pick schedules a submit 200 ms later. The delay exists so the
chosen option paints as selected before the card leaves. The pick is the
option's ordinary `click`, which fires on release, so pressing an option and
dragging off it still cancels.

Anything else the user does inside the 200 ms cancels the pending submit: a
second pick (which replaces the first and restarts the delay), a deselect,
typing, paging, Submit, Dismiss, the card unmounting, or `busy` going true.
Submit stays on the last page, so a user who clicks it anyway cancels the
pending submit and the card sends once.

The rule is narrow so it never submits a card the user is still reviewing:

- Multi-select picks never submit, because one click does not finish a
  multi-select question.
- Custom-answer typing never submits; `Enter` keeps its pager §3.2 behaviour.
- A completing pick on an earlier page does not submit. The user answered out of
  order, and pager §3.2 already sends them to the last page, where Submit is.
- A changed answer on a card that was already complete does not submit, on any
  page. The user came back to revise, the same reason the pager §3.5 row for a
  changed answer does not advance.

The tests that pin this arrive with #17542 in
`website/src/test/QuestionCard.test.tsx`; none exist on main:

- `submits a one-question card from the pick alone, once, after the selection paints`
- `submits only from the pick that answers the last open question`
- `does not submit from an earlier page, even when that pick completes the card`
- `does not submit when the answer on the last page of a complete card is changed`
- `lets a second pick inside the window replace the first, submitting once`

## 4. Case against

Submit stops being the one way a card ends, and an accidental click now answers
the agent. The 200 ms delay is a paint delay, not a window in which a user can
notice a wrong click and cancel it. What limits the risk is the release-time
click and the earlier-page and revise exemptions, which confine the submit to a
click the user finished on the page that already holds Submit.

## 5. Alternatives considered

Keep Submit as the only way a card ends. This costs one click on every
single-select card and is today's behaviour.

Submit from any page whenever a pick completes the card. Rejected because a pick
on an earlier page is usually a correction made while reviewing, and sending
from there skips the last page the user has not looked at again.

A longer delay with a visible countdown and Undo. Rejected for this change: the
answer goes to an agent that resumes at once, so an Undo after send cannot take
it back, and a countdown long enough to react to makes the common case slower
than clicking Submit.

## 6. Acceptance

Proposed 2026-10-07 by Pearcekieser. Not yet accepted. A maintainer accepting
this document is recorded here with their name and the date, and the status
above moves to `accepted` in the same change.
