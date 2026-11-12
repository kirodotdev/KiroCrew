# An Ultra Mode row shows only what needs attention, and an ongoing chat can switch on

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-02
Supersedes: 2026-10-30-ultra-mode-is-marked-with-a-bolt-and-its-tab-is-the-board.md (in part: "The Sessions row counts sessions with the chat icon" and "An Ultra Mode row's own state is one short word")

## Decision

With the Ultra Mode preview on, an Ultra Mode Sessions row shows only what needs
attention, in this order and only when above zero: "N needs you" (approvals and
questions), "N stalled" with a second line naming the crew member stalled longest
and for how long, and "N running". With none of them a finished crew reads "Done";
otherwise the row keeps its usual line. The session total is no longer drawn; the
full counts are in the row's tooltip. An interrupted turn or a stalled goal loop of
the main chat is still said first, as one word.

An ongoing plain chat (one with a message) offers the Ultra Mode chip directly right
of the permission button. Pressing it turns that chat into an Ultra Mode chat through
the same switch a new chat uses. It is one way, and the chip is then gone. It is never
offered on a new chat, an Ultra Mode chat or a crew member's chat.

On the Board the second view is **Workers** (view id unchanged), the member view's
back row reads **Back**, and the welcome page's first step reads **Projects**.

Everything else in the 2026-10-30 entry applies unchanged.

## Why

- A total session count says nothing about what to do next; the row is read to find
  the crew that needs the person.
- A plain chat that grows into a big job should not have to be restarted to get a
  crew.
- "List" and "Goal" did not say what they showed, and "Back to main chat" crowded
  the narrow panel.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/28860 -- the scope the maintainer
  approved from the reviewed mock, filed for the pull request that adds this entry.
