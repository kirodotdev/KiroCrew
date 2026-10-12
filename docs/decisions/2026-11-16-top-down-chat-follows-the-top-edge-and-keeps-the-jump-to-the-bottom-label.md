# Top-down chat follows the top edge in the shared virtualizer, and "Jump to the bottom when you send" keeps its label

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-16

## Decision

The opt-in Top-down chat layout keeps a second follow edge, `followEdge: 'top'`,
in the shared transcript virtualizer, keyed on the follow edge so that one
change can remove the layout. The Settings toggle **Jump to the bottom when you
send** keeps that label and its settings id
`chat.jump-to-the-bottom-when-you-send`. Its description, `scroll_on_send_desc`,
gives the top-down meaning: with Top-down chat on, the jump goes to the top,
where the newest turn is. Embedded hosts follow the Top-down chat setting as
they do now, at every width. `/embed/chat`, the IDE plugin panel, never takes
the phone form (`useIsMobile()` is false under `/embed/`), and a panel inside
the dashboard window reads the window width, not its own. Phones keep the
bottom layout: the chat page in the phone form (`useIsMobile()` true) draws the
composer at the bottom.

## Why

- With the setting off, the default path keeps the behaviour it had before the
  layout.
- The top-down branches have tests and are keyed on the follow edge, so the
  layout can be removed in one change.
- The narrow plugin panel is a desktop surface, not a phone.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/32086#issuecomment-6929452486 --
  the maintainer decision on the pull request that adds the layout.
