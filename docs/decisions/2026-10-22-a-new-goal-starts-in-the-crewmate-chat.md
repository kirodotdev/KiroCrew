# A new crewmate goal starts in the crewmate's chat, and the Goals tab has no "track something" row

Decided by: Zezhen Xu (maintainer, @CrysisDeu)
Date: 2026-10-22

## Decision

On the Goals tab, **Create a goal** is the last row of the goal list. It opens a modal that explains in plain words how the crewmate uses a goal and offers one button, **Tell {name} what to aim for**, which sends an opening message as the user into the crewmate's chat; the crewmate then asks back and proposes the goal on its card for the user to approve. There is no text editor for a new goal on the tab (the editor stays for an existing goal). The **Add something {name} should track** row is removed from the Goals tab.

## Why

- A goal is a conversation, not a form: the crewmate already asks, researches and proposes milestones on a card, so the tab's job is to start that conversation, not to collect text.
- The track row sent a message whose result never came back to the Goals tab (it lands in the work ledger); the same request is one sentence in chat.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/23262 -- the issue carrying the decision.
- https://github.com/kirodotdev/KiroCrew/pull/23280 -- the pull request that implements it, where the maintainer restates the decision.

This supersedes the **Add something {name} should track** row and the empty-editor "+" in `docs/request-for-change/rfc-crewmate-goals.md` § 3.7 and § 3.9, which had no entry in this directory.
