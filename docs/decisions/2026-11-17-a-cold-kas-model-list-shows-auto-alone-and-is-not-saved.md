# A cold KAS model list shows Auto alone and is not saved over the held list

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-17

## Decision

While the model list is cold (no session has recorded a list for the signed-in account
yet, as after a gateway restart), the picker shows "Auto" alone, and the gateway marks
that answer with `"cold": true` on its one row, so the page does not save it over the
list it held, keeps reading every 8 seconds with no failure notice, and keeps a pinned
model's name on the composer chip instead of showing Auto; the mark goes on the relay
that starts on Kiro Crew's own sign-in, which starts no background read, and the
bundle route's background read already refreshes the picker when it lands.

## Why

- A cold answer means no session has recorded a list for the account signed in now,
  and the page cannot always tell that account from the one it held a list for:
  moving from a `kiro-cli` account to a first `kirocrew kas login` in a terminal
  leaves the account the page sees unchanged. "Auto" is the one list that is always
  right.
- Accepted cost: until the first chat after a restart the picker still offers only
  Auto and shows no new text; keeping the list across a restart on the gateway, or a
  hint in the picker, can be a follow-up issue.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/36247#issuecomment-6943211030 -- the
  maintainer's decision on the issue.
- https://github.com/kirodotdev/KiroCrew/issues/36247 -- the issue and its open
  choice.
