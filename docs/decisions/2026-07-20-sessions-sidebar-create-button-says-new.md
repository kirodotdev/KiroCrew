# The Sessions sidebar's primary create button shows the visible label "New"

Decided by: Kyle Seaman (PR #94), reaffirmed by Zezhen Xu (maintainer) on 2026-09-24
Date: 2026-07-20

## Decision

The primary create button in the Sessions sidebar header shows the visible label
"New" (i18n key `pages.chatSidebar.new`). Its `title` stays "New chat", its
accessible name stays "New chat session", and the first row of its caret menu
stays "New chat".

## Why

- The button sits in the header of the panel that lists chats, beside a plus icon
  and a caret. The panel names what "New" makes; repeating "chat" on the button
  says nothing the surrounding surface does not already say.
- The header slot is narrow. The translator note in
  `website/src/i18n/en.context.json` records the width constraint, and compact
  mode already drops the word entirely, so the short form is the one the layout
  is designed around.
- On 2026-09-24 the maintainer confirmed on record that the short form is the intended
  state, and that PR #12291 — which relabelled the button "New chat" — was a
  regression driven by a single first-time-tester friction row (issue #11998),
  not a product decision. Issue #11998 stays closed as decided.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/94 — introduced the "New" label
  (commit 872523ef36).
- https://github.com/kirodotdev/KiroCrew/pull/12291 — the reversal this entry
  records as a regression.
- https://github.com/kirodotdev/KiroCrew/issues/11998 — the nightly friction
  report that drove it.
- https://github.com/kirodotdev/KiroCrew/pull/13194#issuecomment-5807378325 —
  the maintainer's dated statement of the decision (2026-09-24), the record this
  entry's reaffirmation rests on.
